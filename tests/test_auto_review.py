import asyncio
import json
import pytest
from pathlib import Path
from pydantic_ai.messages import ToolReturnPart, ThinkingPart, ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from myrk.conversation import Conversation, Task, TaskScope
from myrk.permissions import ApprovalSettings


def test_auto_review_independent_allows_exact_action_and_counts_requests(tmp_path: Path):
    folder = tmp_path / 'task'
    folder.mkdir()
    target = tmp_path / 'outside.txt'
    task = Task(goal='请在指定位置创建文字', scope=TaskScope(root=str(folder), allowed_actions=[], provider='deepseek', recipients=['deepseek']), request_limit=10,
                approval=ApprovalSettings(mode='auto_review', reviewer='assistant'))
    async def main(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name='create_text', json_args=json.dumps({'path': str(target), 'content': 'approved-text'}), tool_call_id='create')}
        else:
            yield '完成。'
    async def reviewer(messages, info):
        rendered = repr(messages)
        assert 'outside.txt' in rendered and 'approved-text' in rendered
        assert not any(isinstance(part, ThinkingPart) for message in messages for part in message.parts)
        assert set(tool.name for tool in info.function_tools) == {'read_text', 'run_command'}
        await asyncio.sleep(.02)
        yield json.dumps({'decision':'allow','risk':'low','authorization':'high','rule':'general','reason':'用户明确要求这一个小文件，创建不会覆盖已有内容。','same_denied_outcome':False})
    async def run():
        turn = await Conversation(tmp_path / 'records' / 'session.json').ask(task.goal, document=None, scope=task.scope, task=task,
            model=FunctionModel(stream_function=main), review_model=FunctionModel(stream_function=reviewer))
        assert turn.status == 'completed', turn.error
        action = turn.actions[-1]
        assert action.status == 'completed', action.error
        assert action.approval_basis == 'assistant'
        assert action.review.status == 'allowed'
        assert action.review.attempts == task.review_requests == 1
        assert task.requests == 3
        assert task.review_seconds > 0 and task.active_seconds >= task.review_seconds
        assert target.read_text(encoding='utf-8') == 'approved-text'
    asyncio.run(run())


def test_auto_review_readonly_command_cannot_write_evidence(tmp_path: Path):
    from myrk.executor import POWERSHELL
    folder = tmp_path / 'task'; folder.mkdir()
    protected = folder / 'proof.txt'; protected.write_text('unchanged', encoding='utf-8')
    target = tmp_path / 'approved.txt'
    task = Task(goal='创建指定文字，先核查材料', scope=TaskScope(root=str(folder), allowed_actions=[], provider='deepseek', recipients=['deepseek']), request_limit=10,
                approval=ApprovalSettings(mode='auto_review', reviewer='assistant'))
    async def main(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name='create_text', json_args=json.dumps({'path': str(target), 'content': 'done'}), tool_call_id='create')}
        else: yield '完成。'
    async def reviewer(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            script = "[Console]::WriteLine([IO.File]::ReadAllText('" + str(protected).replace("'", "''") + "')); [IO.File]::WriteAllText('" + str(protected).replace("'", "''") + "','must-not-write')"
            yield {0: DeltaToolCall(name='run_command', json_args=json.dumps({'program':POWERSHELL,'arguments':['-NoProfile','-Command',script]}), tool_call_id='inspect')}
        else:
            assert 'unchanged' in str(results[-1].content)
            assert 'exitCode' in str(results[-1].content)
            yield json.dumps({'decision':'allow','risk':'low','authorization':'high','rule':'general','reason':'已核查只读材料，小范围新增与用户要求一致。','same_denied_outcome':False})
    async def run():
        turn = await Conversation(tmp_path / 'records' / 'session.json').ask(task.goal,document=None,task=task,scope=task.scope,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer))
        assert turn.status == 'completed', turn.error
        assert turn.actions[-1].status == 'completed', turn.actions[-1].error
        assert protected.read_text(encoding='utf-8') == 'unchanged'
        assert task.review_requests == 2
    asyncio.run(run())


def test_user_can_authorize_one_exact_denied_action_for_independent_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from pydantic_ai import models
    from tests.support.agent_acceptance.replay import OfflineSession, Reply

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)

    folder = tmp_path / "task"
    folder.mkdir()
    target = tmp_path / "outside.txt"
    main_session = OfflineSession("deepseek", [
        Reply(tool="create_text", arguments={"path": str(target), "content": "once"}), Reply(text="本轮结束。"),
        Reply(tool="create_text", arguments={"path": str(target), "content": "once"}), Reply(text="再审完成。")])
    review_session = OfflineSession("deepseek", [Reply(text=json.dumps({
        "decision": decision, "risk": "high", "authorization": authorization, "rule": "general",
        "reason": "本人尚未明确同意具体风险，需确切授权后再判断。" if decision == "deny" else "本人已确切授权本动作再审，独立核对后允许本次原动作。", "same_denied_outcome": True}))
        for decision, authorization in [("deny", "low"), ("allow", "high")]])
    task = Task(goal="创建指定文字", scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]),
        request_limit=6, provider_config=main_session.config, approval=ApprovalSettings(mode="auto_review", reviewer="assistant"))

    async def run():
        conversation = Conversation(tmp_path / "records" / "session.json")
        async with main_session.model() as main_model, review_session.model() as review_model:
            first = await conversation.ask(task.goal, document=None, task=task, scope=task.scope, model=main_model, review_model=review_model)
            assert first.status == "completed", first.error
            assert first.actions[-1].status == "denied"
            assert not target.exists()
            denied = first.actions[-1]
            chosen = conversation.authorize_denied_retry(task.id, denied.id)
            assert chosen.id == task.id and denied.review.override_used
            assert chosen.requests == chosen.total_requests == first.task.requests == 3
            second = await conversation.ask("对本人刚选定动作再审一次", document=None, task=chosen, scope=chosen.scope,
                model=main_model, review_model=review_model)
            assert second.status == "completed", second.error
            assert second.task.id == first.task.id == task.id
            assert second.task.requests == second.task.total_requests == second.task.request_limit == 6
            assert second.task.review_requests == 2 and first.task.requests == first.task.total_requests == 3
            assert second.actions[-1].status == "completed", second.actions[-1].error
            assert second.actions[-1].review.user_override
            assert target.read_text(encoding="utf-8") == "once"
            assert len(main_session.exchanges) == len(main_session.sdk_inputs) == 4
            assert len(review_session.exchanges) == len(review_session.sdk_inputs) == 2
            review_inputs = [json.loads(part["content"]) for item in review_session.sdk_inputs
                for message in item["messages"] for part in message["parts"]
                if part["part_kind"] == "user-prompt" and part["content"].startswith("{")]
            assert len(review_inputs) == 2
            assert [item["one_time_user_authorization"] for item in review_inputs] == [False, True]
            assert review_inputs[0]["action"]["prepared_parameters"] == review_inputs[1]["action"]["prepared_parameters"]
            assert review_inputs[1]["action"]["target"] == str(target)
            with pytest.raises(ValueError):
                conversation.authorize_denied_retry(task.id, denied.id)
        saved = Conversation.open(conversation.path)
        assert [turn.task.requests for turn in saved.turns] == [3, 6]
        assert saved.turns[-1].task.id == task.id and saved.turns[-1].task.review_requests == 2
    asyncio.run(run())

from pydantic_ai.exceptions import ModelHTTPError


@pytest.mark.parametrize('responses,expected,requests', [
    ([503, 'allow'], 'completed', 2),
    (['invalid', 'allow'], 'completed', 2),
    ([401], 'failed', 1),
    ([404], 'failed', 1),
    ([402], 'failed', 1),
    ([503, 503, 503], 'failed', 3),
    (['invalid', 'invalid', 'invalid'], 'failed', 3),
])
def test_review_retries_only_recoverable_results_and_preserves_total_limit(tmp_path: Path, responses, expected, requests):
    folder=tmp_path/'task';folder.mkdir()
    target=tmp_path/'result.txt'
    task=Task(goal='创建指定小文件',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=12,
              approval=ApprovalSettings(mode='auto_review',reviewer='assistant'))
    async def main(messages,_info):
        if not any(isinstance(part,ToolReturnPart) for message in messages for part in message.parts):
            yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(target),'content':'ok'}),tool_call_id='create')}
        else: yield '结束。'
    count=0
    async def reviewer(messages,_info):
        nonlocal count
        response=responses[count];count+=1
        if isinstance(response,int):raise ModelHTTPError(response,'same-review-service')
        if response=='invalid':yield '安全'
        else:yield json.dumps({'decision':'allow','risk':'low','authorization':'high','rule':'general','reason':'确切文件和副作用已核实，符合用户的具体要求。','same_denied_outcome':False})
    async def run():
        turn=await Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,scope=task.scope,task=task,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer))
        action=turn.actions[-1]
        assert action.status==expected,action.error
        assert task.review_requests==requests
        assert task.requests==requests+2
        assert action.review.status==('allowed' if expected=='completed' else 'failed')
        assert target.exists()==(expected=='completed')
    asyncio.run(run())


@pytest.mark.parametrize('risk,authorization,rule', [('critical','high','general'),('high','low','general'),('low','high','explicit_forbid'),('medium','medium','sensitive_export'),('low','high','destructive_unverified')])
def test_reviewer_allow_cannot_override_specific_risk_or_authorization_policy(tmp_path: Path,risk,authorization,rule):
    folder=tmp_path/'task';folder.mkdir()
    target=tmp_path/'blocked.txt'
    task=Task(goal='创建文字',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=10,
              approval=ApprovalSettings(mode='auto_review',reviewer='assistant'))
    async def main(messages,_info):
        if not any(isinstance(part,ToolReturnPart) for message in messages for part in message.parts):
            yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(target),'content':'no'}),tool_call_id='create')}
        else:yield '拒绝后结束。'
    async def reviewer(messages,_info):
        yield json.dumps({'decision':'allow','risk':risk,'authorization':authorization,'rule':rule,'reason':'按已核对的具体风险与授权材料进行判断。','same_denied_outcome':False})
    async def run():
        turn=await Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,scope=task.scope,task=task,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer))
        assert turn.actions[-1].status=='denied'
        assert turn.actions[-1].review.status=='denied'
        assert not target.exists()
    asyncio.run(run())


def test_three_consecutive_denials_interrupt_but_failure_is_not_denial(tmp_path: Path):
    folder=tmp_path/'task';folder.mkdir()
    task=Task(goal='进行有限动作',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=20,
              approval=ApprovalSettings(mode='auto_review',reviewer='assistant'))
    async def main(messages,_info):
        results=[part for message in messages for part in message.parts if isinstance(part,ToolReturnPart)]
        yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(tmp_path/f'result{len(results)}.txt'),'content':'no'}),tool_call_id='create'+str(len(results)))}
    async def reviewer(messages,_info):
        yield json.dumps({'decision':'deny','risk':'high','authorization':'low','rule':'general','reason':'未获确切副作用授权，只能选择实质更安全的替代。','same_denied_outcome':False})
    async def run():
        turn=await Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,scope=task.scope,task=task,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer))
        assert turn.status=='limited'
        assert '连续3次' in turn.error
        assert len(turn.actions)==3 and all(action.status=='denied' for action in turn.actions)
        assert task.review_requests==3
        assert not list(tmp_path.glob('result*.txt'))
    asyncio.run(run())


def test_shared_review_deadline_covers_transient_wait_and_does_not_pause_task_time(tmp_path: Path):
    folder=tmp_path/'task';folder.mkdir()
    task=Task(goal='创建文字',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=10,
              review_timeout_seconds=.06,approval=ApprovalSettings(mode='auto_review',reviewer='assistant'))
    async def main(messages,_info):
        if not any(isinstance(part,ToolReturnPart) for message in messages for part in message.parts):
            yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(tmp_path/'no.txt'),'content':'no'}),tool_call_id='create')}
        else:yield '审核未完成。'
    async def reviewer(messages,_info):
        raise ModelHTTPError(429,'review',body={'retry_after':1})
        yield ''
    async def run():
        turn=await Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,scope=task.scope,task=task,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer))
        action=turn.actions[-1]
        assert action.status=='failed'
        assert action.review.status=='timeout'
        assert task.review_requests==1
        assert .04 <= task.review_seconds < .5
        assert task.active_seconds >= task.review_seconds
        assert not (tmp_path/'no.txt').exists()
    asyncio.run(run())


def test_review_exhausts_original_task_requests_before_three_attempts(tmp_path: Path):
    folder=tmp_path/'task';folder.mkdir()
    task=Task(goal='创建文字',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=2,
              approval=ApprovalSettings(mode='auto_review',reviewer='assistant'))
    async def main(messages,_info):
        yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(tmp_path/'no.txt'),'content':'no'}),tool_call_id='create')}
    async def reviewer(messages,_info):
        yield '无效结果'
    async def run():
        turn=await Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,scope=task.scope,task=task,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer))
        assert turn.status=='limited'
        assert task.requests==2 and task.review_requests==1
        assert not (tmp_path/'no.txt').exists()
    asyncio.run(run())


def test_stop_during_review_invalidates_late_allow(tmp_path: Path):
    from pydantic_ai import CancellationToken
    folder=tmp_path/'task';folder.mkdir()
    task=Task(goal='创建文字',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=10,
              approval=ApprovalSettings(mode='auto_review',reviewer='assistant'))
    token=CancellationToken()
    async def main(messages,_info):
        yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(tmp_path/'no.txt'),'content':'no'}),tool_call_id='create')}
    async def reviewer(messages,_info):
        token.cancel()
        await asyncio.sleep(.02)
        yield json.dumps({'decision':'allow','risk':'low','authorization':'high','rule':'general','reason':'即使这份结果迟到允许也不能让旧动作执行。','same_denied_outcome':False})
    async def run():
        turn=await Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,scope=task.scope,task=task,cancellation_token=token,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer))
        assert turn.status=='cancelled'
        assert turn.actions[-1].status=='cancelled'
        assert turn.actions[-1].review.status=='cancelled'
        assert not (tmp_path/'no.txt').exists()
    asyncio.run(run())


def test_service_retry_denied_action_reopens_same_task_and_still_reviews(tmp_path: Path):
    from tests.test_desktop_service import connection
    folder=tmp_path/'task';folder.mkdir()
    with connection(tmp_path/'records',module='tests.auto_review_fixture') as channel:
        channel.send({'action':'configure','approval_mode':'auto_review'})
        assert channel.receive()['event']=='configured'
        channel.send({'action':'new'})
        assert channel.receive()['event']=='conversation'
        channel.send({'action':'configure','key':'synthetic-no-paid-service'})
        assert channel.receive()['event']=='configured'
        channel.send({'action':'send','prompt':'创建指定小文件','scope':{'root':str(folder),'allowed_actions':[],'provider':'deepseek','recipients':['deepseek']}})
        while (first:=channel.receive())['event']!='finished':pass
        assert first['turn']['status']=='completed',first
        action=first['turn']['actions'][-1]
        assert action['review']['status']=='denied' and not (tmp_path/'outside.txt').exists()
        channel.send({'action':'retry_denied_action','path':first['path'],'task_id':action['task_id'],'action_id':action['id']})
        while (second:=channel.receive())['event']!='finished':
            assert second['event']!='error',second
        assert second['turn']['task']['id']==action['task_id']
        assert second['turn']['actions'][-1]['review']['user_override'] is True
        assert second['turn']['actions'][-1]['status']=='completed',second
        assert (tmp_path/'outside.txt').read_text(encoding='utf-8')=='reviewed-once'
        channel.send({'action':'retry_denied_action','path':first['path'],'task_id':action['task_id'],'action_id':action['id']})
        assert channel.receive()['event']=='error'


@pytest.mark.parametrize('mandatory,expected_human', [(False,True),(True,False)])
def test_only_optional_review_capacity_failure_can_return_to_human(tmp_path: Path,mandatory,expected_human):
    folder=tmp_path/'task';folder.mkdir()
    approval=ApprovalSettings(mode='auto_review',reviewer='assistant',review_mandatory=mandatory)
    task=Task(goal='创建指定文字',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=10,approval=approval)
    called=[]
    async def decide(action):
        called.append(action)
        assert action.review.human_fallback
        return False
    async def main(messages,_info):
        if not any(isinstance(part,ToolReturnPart) for message in messages for part in message.parts):
            yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(tmp_path/'no.txt'),'content':'x'*600_000}),tool_call_id='create')}
        else:yield '未执行。'
    async def reviewer(messages,_info):
        raise AssertionError('too much required material must not be dispatched')
        yield ''
    async def run():
        turn=await Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,task=task,scope=task.scope,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer),approve_action=decide)
        assert bool(called)==expected_human
        assert turn.actions[-1].status==('denied' if expected_human else 'failed')
        assert task.review_requests==0
        assert not (tmp_path/'no.txt').exists()
    asyncio.run(run())


@pytest.mark.parametrize('changes,catalog_ok,expected_model,refreshes,review_ok', [
    ({},True,'codex-auto-review',1,True),
    ({'available_models':[]},True,'parent-model',0,True),
    ({'available_models':['codex-auto-review']},True,'codex-auto-review',0,True),
    ({'preferred_review_model':'unlisted-preference'},True,'parent-model',0,True),
    ({'basic_session_source':True},True,'parent-model',0,True),
    ({'model_catalog_url':'http://127.0.0.1/provider-catalog'},True,'parent-model',0,True),
    ({},False,'parent-model',1,True),
    ({'review_model_override':'explicit-missing','available_models':[]},True,'explicit-missing',0,False),
])
def test_same_provider_special_review_catalog_refreshes_only_once_per_chat(tmp_path: Path,changes,catalog_ok,expected_model,refreshes,review_ok):
    import threading
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    from tests.test_desktop_service import connection
    requests=[]
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            requests.append(('catalog',self.path,self.headers.get('Authorization')))
            if not catalog_ok:
                payload=b'{"error":{"message":"temporary catalog failure","type":"server_error"}}'
                self.send_response(500);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);return
            payload=json.dumps({'object':'list','data':[{'id':'parent-model','object':'model','created':0,'owned_by':'local'},{'id':'codex-auto-review','object':'model','created':0,'owned_by':'local'}]}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((body['model'],body,self.headers.get('Authorization')))
            if {tool['function']['name'] for tool in body.get('tools',[])}=={'read_text','run_command'}:
                if not review_ok:
                    payload=b'{"error":{"message":"explicit model missing","type":"invalid_request_error"}}'
                    self.send_response(404);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);return
                text=json.dumps({'decision':'deny','risk':'high','authorization':'low','rule':'general','reason':'独立会话未找到对应具体副作用的明确授权。','same_denied_outcome':False})
                delta={'content':text};finish='stop'
                assert {tool['function']['name'] for tool in body['tools']}=={'read_text','run_command'}
                assert body['reasoning_effort']=='low'
            else:
                results=[message for message in body['messages'] if message['role']=='tool']
                if len(results)<2:
                    delta={'tool_calls':[{'index':0,'id':'create'+str(len(results)),'type':'function','function':{'name':'create_text','arguments':json.dumps({'path':str(tmp_path/f'outside{len(results)}.txt'),'content':'no'})}}]};finish='tool_calls'
                else:delta={'content':'两项均已拒绝，结束。'};finish='stop'
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            for value,reason in [(delta,None),({},finish)]:
                chunk={'id':'local-completion','object':'chat.completion.chunk','created':0,'model':body['model'],'choices':[{'index':0,'delta':value,'finish_reason':reason}]}
                self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
    server=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    folder=tmp_path/'task';folder.mkdir()
    try:
        with connection(tmp_path/'records',module='tests.auto_review_http_fixture') as channel:
            config={'name':'本机预设服务','base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'parent-model','protocol':'openai-chat','context_window_override':128000,'preferred_review_model':'codex-auto-review','review_reasoning_efforts':['low','medium'],**changes}
            channel.send({'action':'configure','approval_mode':'auto_review'})
            assert channel.receive()['event']=='configured'
            channel.send({'action':'new'})
            assert channel.receive()['event']=='conversation'
            channel.send({'action':'configure','key':'local-only-key','provider_config':config})
            assert channel.receive()['event']=='configured'
            channel.send({'action':'send','prompt':'核对两个待批动作','scope':{'root':str(folder),'allowed_actions':[],'provider':'openai-compatible','recipients':['openai-compatible']}})
            while (finished:=channel.receive())['event']!='finished':pass
            assert finished['turn']['status']=='completed',finished
            assert finished['turn']['task']['requests']==5
            assert finished['turn']['task']['review_requests']==2
            assert all(action['review']['model']==expected_model for action in finished['turn']['actions'])
            assert all(action['status']==('denied' if review_ok else 'failed') for action in finished['turn']['actions'])
        assert sum(entry[0]=='catalog' for entry in requests)==refreshes
        assert [entry[0] for entry in requests if entry[0]!='catalog']==['parent-model',expected_model,'parent-model',expected_model,'parent-model']
        assert all(entry[2]=='Bearer local-only-key' for entry in requests)
        assert not list(tmp_path.glob('outside*.txt'))
    finally:server.shutdown();server.server_close();worker.join(timeout=2)


def test_reviewer_receives_visible_user_assistant_tool_evidence_without_hidden_thinking(tmp_path: Path):
    from pydantic_ai.models.function import DeltaThinkingPart
    folder=tmp_path/'task';folder.mkdir()
    task=Task(goal='按公开要求创建文字',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=10,approval=ApprovalSettings(mode='auto_review',reviewer='assistant'))
    async def first(messages,_info):
        yield {0:DeltaThinkingPart(content='hidden-secret-thinking-9273')}
        yield '公开计划：只创建指定小文件。'
    async def main(messages,_info):
        if not any(isinstance(part,ToolReturnPart) for message in messages for part in message.parts):
            yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(tmp_path/'visible.txt'),'content':'visible-body'}),tool_call_id='create')}
        else:yield '结束。'
    async def reviewer(messages,_info):
        rendered=str(messages[-1].parts[0].content)
        assert '公开计划' in rendered and 'visible-body' in rendered and 'create_text' in rendered
        assert 'hidden-secret-thinking-9273' not in rendered
        yield json.dumps({'decision':'deny','risk':'high','authorization':'low','rule':'general','reason':'可见材料已核对，但具体风险仍未得到明确授权。','same_denied_outcome':False})
    async def run():
        conversation=Conversation(tmp_path/'records'/'session.json')
        await conversation.ask(task.goal,document=None,scope=task.scope,task=task,model=FunctionModel(stream_function=first))
        turn=await conversation.ask('按上面的公开计划处理',document=None,scope=task.scope,task=task,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer))
        assert turn.status=='completed',turn.error
        assert all(not isinstance(part,ThinkingPart) for message in turn.actions[-1].review.messages for part in message.parts)
        assert not (tmp_path/'visible.txt').exists()
    asyncio.run(run())


def test_ten_denials_in_last_fifty_completed_reviews_interrupt_after_safer_alternatives(tmp_path: Path):
    folder=tmp_path/'task';folder.mkdir()
    task=Task(goal='处理有不同风险的动作',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=80,approval=ApprovalSettings(mode='auto_review',reviewer='assistant'))
    async def main(messages,_info):
        results=[part for message in messages for part in message.parts if isinstance(part,ToolReturnPart)]
        yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(tmp_path/f'variant{len(results)}.txt'),'content':'small-safe-alternative' if len(results)%2 else 'unapproved-effect'}),tool_call_id='create'+str(len(results)))}
    count=0
    async def reviewer(messages,_info):
        nonlocal count
        deny=count%2==0;count+=1
        yield json.dumps({'decision':'deny' if deny else 'allow','risk':'high' if deny else 'low','authorization':'low' if deny else 'high','rule':'general','reason':'此项确切影响及具体风险尚未取得用户授权。' if deny else '这项是实质更安全的独立替代，不产生先前被拒的效果。','same_denied_outcome':False})
    async def run():
        turn=await Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,scope=task.scope,task=task,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer))
        assert turn.status=='limited',turn.error
        assert task.review_requests==19
        assert len(turn.completed_reviews)==19
        assert sum(action.status=='denied' for action in turn.actions)==10
        assert sum(action.status=='completed' for action in turn.actions)==9
        assert len(list(tmp_path.glob('variant*.txt')))==9
    asyncio.run(run())


def test_cyber_model_specialty_uses_one_denial_threshold(tmp_path: Path):
    from myrk.provider import ProviderConfig
    folder=tmp_path/'task';folder.mkdir()
    task=Task(goal='核对一项动作',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=10,provider_config=ProviderConfig(model_specialty='cyber'),approval=ApprovalSettings(mode='auto_review',reviewer='assistant'))
    async def main(messages,_info):
        yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(tmp_path/'no.txt'),'content':'no'}),tool_call_id='create')}
    async def reviewer(messages,_info):
        yield json.dumps({'decision':'deny','risk':'high','authorization':'low','rule':'general','reason':'对应具体影响尚未授权，按安全专长方式中断。','same_denied_outcome':False})
    async def run():
        turn=await Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,scope=task.scope,task=task,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer))
        assert turn.status=='limited'
        assert len(turn.actions)==1 and turn.actions[0].status=='denied'
        assert task.review_requests==1
    asyncio.run(run())


@pytest.mark.parametrize('allow', [True,False])
def test_pending_native_network_review_can_read_evidence_without_deadlock(tmp_path: Path,allow):
    import sys
    import threading
    from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
    received=[]
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(204);self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    folder=tmp_path/'task';folder.mkdir()
    (folder/'proof.txt').write_text('proof-necessary-evidence-734',encoding='utf-8')
    task=Task(goal='向已指定本机接收方发送一次核准样例',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=10,approval=ApprovalSettings(mode='auto_review'))
    script="import os,http.client,urllib.parse,sys;p=urllib.parse.urlsplit(os.environ['HTTP_PROXY']);c=http.client.HTTPConnection(p.hostname,p.port);c.request('POST',sys.argv[1],b'exact-network-body');print(c.getresponse().status)"
    async def main(messages,_info):
        if not any(isinstance(part,ToolReturnPart) for message in messages for part in message.parts):
            yield {0:DeltaToolCall(name='run_command',json_args=json.dumps({'program':sys.executable,'arguments':['-c',script,f'http://127.0.0.1:{server.server_port}/receive']}),tool_call_id='network-command')}
        else:yield '连接决定和真实输出已保留。'
    async def reviewer(messages,_info):
        results=[part for message in messages for part in message.parts if isinstance(part,ToolReturnPart)]
        if not results:
            assert 'exact-network-body' in str(messages) and '127.0.0.1' in str(messages)
            yield {0:DeltaToolCall(name='read_text',json_args='{"path":"proof.txt"}',tool_call_id='review-read')}
        else:
            assert 'proof-necessary-evidence-734' in str(results[-1].content)
            yield json.dumps({'decision':'allow' if allow else 'deny','risk':'low' if allow else 'high','authorization':'high' if allow else 'low','rule':'general','reason':'已在原命令等待连接决定时独立读回证据，核实确切目标与资料。','same_denied_outcome':False})
    async def run():
        turn=await asyncio.wait_for(Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,task=task,scope=task.scope,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer)),20)
        assert turn.status=='completed',turn.error
        command=next(action for action in turn.actions if action.kind=='command')
        network=next(action for action in turn.actions if action.kind=='network')
        assert network.status==('completed' if allow else 'denied'),network.error
        assert command.stdout.strip()==('204' if allow else '403')
        assert command.cleanup_complete and command.sandbox_type=='windowsMxc'
        assert network.approval_settings.reviewer=='assistant'
        assert task.review_requests==2
        assert received==([b'exact-network-body'] if allow else [])
    try:asyncio.run(run())
    finally:server.shutdown();server.server_close();worker.join(timeout=2)


def test_changed_user_authorization_invalidates_old_allow_and_uses_fresh_material(tmp_path: Path):
    from myrk.conversation import ConversationRecord, Supplement
    folder=tmp_path/'task';folder.mkdir()
    task=Task(goal='创建原定文字',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=10,approval=ApprovalSettings(mode='auto_review'))
    path = tmp_path / 'records' / 'session.json'
    supplement = Supplement(text='先暂停这个原动作，按新要求重新准备。')
    withdrawn = Supplement(text='已经撤回的旧授权：始终允许原操作。', revoked_at='2026-11-18T00:00:00+00:00')
    task.supplements.append(withdrawn)
    main_count=0
    async def main(messages,_info):
        nonlocal main_count
        main_count+=1
        if main_count==1:
            yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(tmp_path/'stale.txt'),'content':'must-not-write'}),tool_call_id='create')}
        else:yield '已采用新要求，旧步骤未开始。'
    count=0
    async def reviewer(messages,_info):
        nonlocal count
        count+=1
        assert withdrawn.text not in str(messages)
        if count == 1:
            task.supplements.append(supplement)
        else:
            assert supplement.text in str(messages)
            saved = ConversationRecord.model_validate_json(path.read_text(encoding='utf-8'))
            saved_task = saved.turns[-1].task
            assert saved_task is not None
            assert saved_task.supplements[-1].selected and saved_task.supplements[-1].applied_request is None
        yield json.dumps({'decision':'allow','risk':'low','authorization':'high','rule':'general','reason':'即使返回允许，也必须核对本次材料的当前授权背景。','same_denied_outcome':False})
    async def run():
        turn=await Conversation(path).ask(task.goal,document=None,task=task,scope=task.scope,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer))
        assert turn.status=='completed',turn.error
        assert turn.actions[0].status=='conflict'
        assert turn.actions[0].review.attempts==2
        assert turn.actions[0].review.status=='context_changed'
        assert not (tmp_path/'stale.txt').exists()
        assert supplement.applied_request == turn.request_attempts[-1].number
        assert not withdrawn.selected and withdrawn.applied_request is None
    asyncio.run(run())


def test_explicit_memory_human_rule_captures_user_before_action_binding(tmp_path: Path):
    from myrk.memory import MemoryStore
    folder=tmp_path/'task';folder.mkdir()
    conversation=Conversation(tmp_path/'records'/'session.json')
    memory=MemoryStore(tmp_path/'records'/'memories')
    record=memory.read();record.approve_writes=True;memory.save(record)
    task=Task(goal='保存一条事实前必须由我本人批准',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=10,approval=ApprovalSettings(mode='auto_review'))
    approvals=[]
    async def human(action):
        approvals.append(action)
        assert action.approval_settings.reviewer=='user'
        assert action.approval_settings.condition=='on_request'
        return False
    async def main(messages,_info):
        if not any(isinstance(part,ToolReturnPart) for message in messages for part in message.parts):
            yield {0:DeltaToolCall(name='memory',json_args='{"action":"add","text":"本人批准后才保存的事实。"}',tool_call_id='memory')}
        else:yield '拒绝后没有改变事实笔记。'
    async def reviewer(messages,_info):
        raise AssertionError('explicit human-only rule must never use an independent reviewer')
        yield ''
    async def run():
        turn=await conversation.ask(task.goal,document=None,task=task,scope=task.scope,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer),approve_action=human)
        assert turn.status=='completed',turn.error
        assert len(approvals)==1
        assert turn.actions[0].status=='denied' and turn.actions[0].review is None
        assert memory.read().facts==[]
    asyncio.run(run())


@pytest.mark.parametrize('outcome', ['allowed', 'timeout', 'stopped'])
def test_review_still_consumes_task_time_while_another_action_waits_for_human(tmp_path: Path, outcome: str):
    from myrk.memory import MemoryStore
    from pydantic_ai import CancellationToken
    folder=tmp_path/'task';folder.mkdir()
    memory=MemoryStore(tmp_path/'records'/'memories');record=memory.read();record.approve_writes=True;memory.save(record)
    task=Task(goal='核对独立审查和本人等待的共同时间',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),active_seconds_limit=.04,review_timeout_seconds=.1 if outcome=='timeout' else 2,approval=ApprovalSettings(mode='auto_review'))
    token=CancellationToken()
    reviewer_started=asyncio.Event()
    async def main(messages,_info):
        if not any(isinstance(part,ToolReturnPart) for message in messages for part in message.parts):
            yield {0:DeltaToolCall(name='memory',json_args='{"action":"add","text":"本人决定的事实。"}',tool_call_id='human'),1:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(tmp_path/'no.txt'),'content':'reviewed'}),tool_call_id='review')}
        else:yield '按实际动作记录结束本轮。'
    async def human(action):
        await asyncio.sleep(.22)
        return False
    async def reviewer(messages,_info):
        reviewer_started.set()
        await asyncio.sleep(.12 if outcome=='allowed' else 1)
        yield json.dumps({'decision':'allow','risk':'low','authorization':'high','rule':'general','reason':'这个确切小文件与用户要求一致，影响范围已经核实。','same_denied_outcome':False})
    async def stop_review():
        await reviewer_started.wait()
        await asyncio.sleep(.08)
        token.cancel()
    async def run():
        stopper=asyncio.create_task(stop_review()) if outcome=='stopped' else None
        try:
            turn=await Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,task=task,scope=task.scope,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer),approve_action=human,cancellation_token=token)
        finally:
            if stopper is not None:
                stopper.cancel()
                await asyncio.gather(stopper,return_exceptions=True)
        assert turn.status==('cancelled' if outcome=='stopped' else 'completed'),turn.error
        reviewed=next(action for action in turn.actions if action.kind=='create')
        assert reviewed.review.status==('cancelled' if outcome=='stopped' else outcome)
        assert reviewed.status==('completed' if outcome=='allowed' else 'failed' if outcome=='timeout' else 'cancelled')
        assert reviewed.review.seconds>.04
        assert task.active_seconds>=reviewed.review.seconds
        assert task.active_seconds_limit is None
        assert (tmp_path/'no.txt').exists()==(outcome=='allowed')
        assert reviewed.review.request_attempts[0].status==('completed' if outcome=='allowed' else 'failed')
    asyncio.run(run())


def test_compact_review_material_preserves_large_exact_action_without_false_capacity_fallback(tmp_path: Path):
    folder=tmp_path/'task';folder.mkdir()
    content='retained-exact-text-'+'x'*200_000
    task=Task(goal='核对指定的大文字新建内容',scope=TaskScope(root=str(folder),allowed_actions=[],provider='deepseek',recipients=['deepseek']),request_limit=10,approval=ApprovalSettings(mode='auto_review'))
    async def main(messages,_info):
        if not any(isinstance(part,ToolReturnPart) for message in messages for part in message.parts):
            yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(tmp_path/'large.txt'),'content':content}),tool_call_id='large-create')}
        else:yield '未执行。'
    async def reviewer(messages,_info):
        payload=json.loads(messages[-1].parts[0].content)
        assert payload['action']['prepared_parameters']['content']==content
        assert len(messages[-1].parts[0].content.encode('utf-8'))<500_000
        yield json.dumps({'decision':'deny','risk':'high','authorization':'low','rule':'general','reason':'已核对完整确切动作，但本次风险尚未获得足够授权。','same_denied_outcome':False})
    async def human(action):
        raise AssertionError('duplicated exact content is not a minimum-material capacity failure')
    async def run():
        turn=await Conversation(tmp_path/'records'/'session.json').ask(task.goal,document=None,task=task,scope=task.scope,model=FunctionModel(stream_function=main),review_model=FunctionModel(stream_function=reviewer),approve_action=human)
        assert turn.status=='completed' and turn.error is None and turn.answer=='未执行。'
        assert turn.actions[-1].status=='denied' and turn.actions[-1].review.status=='denied'
        assert task.review_requests==1
        assert not (tmp_path/'large.txt').exists()
    asyncio.run(run())
