"""Full access and native connection decisions through public task entry points."""

import asyncio
import json
from pathlib import Path
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from myrk.conversation import Conversation, Task, TaskScope
from myrk.permissions import AccessSettings, ApprovalSettings, NetworkSettings
from myrk.executor import OfficialExecutor, ExecutorError


@pytest.fixture
def receiver():
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204)
            self.end_headers()
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port, received
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_full_access_runs_files_and_commands_outside_the_task_without_ordinary_approval(tmp_path: Path) -> None:
    folder = tmp_path / "task"
    folder.mkdir()
    outside = tmp_path / "outside.txt"
    scope = TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="完全访问跨位置写入", scope=scope, request_limit=10, approval=ApprovalSettings(mode="full_access"))
    approvals = []
    async def approve(action):
        approvals.append(action)
        return False
    async def model(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("create_text", {"path": str(outside), "content": "outside-created"}), ("read_text", {"path": str(outside)}),
                 ("run_command", {"program": sys.executable, "arguments": ["-c", "from pathlib import Path;import sys;Path(sys.argv[1]).write_text('full-command-result');print('full-output')", str(outside)]})]
        if len(results) < len(steps):
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"full-{len(results)}")}
        else:
            yield "跨位置完成。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None, task=task, scope=scope, model=FunctionModel(stream_function=model), approve_action=approve)
        assert turn.status == "completed", turn.error
        assert not approvals
        assert [action.status for action in turn.actions] == ["completed"] * 3
        assert [action.sandbox_type for action in turn.actions] == ["none"] * 3
        assert outside.read_text() == "full-command-result"
        command = turn.actions[-1]
        assert command.stdout == "full-output\n" and command.cleanup_complete is True
        assert json.loads(Path(command.receipt_path).read_text())["sandbox_type"] == "none"
    asyncio.run(run())


def test_command_helper_routes_each_native_connection_to_captured_reviewer(tmp_path: Path, receiver) -> None:
    port, received = receiver
    approvals = []
    task = Task(goal="逐次判断本机测试连接", scope=TaskScope(root=str(tmp_path), allowed_actions=[], provider="deepseek", recipients=["deepseek"]), request_limit=10,
                approval=ApprovalSettings(access=AccessSettings(network=NetworkSettings(allow_local_binding=True))))
    conversation = Conversation(tmp_path / "records" / "session.json")
    script = "import os,http.client,urllib.parse,sys;p=urllib.parse.urlsplit(os.environ['HTTP_PROXY']);c=http.client.HTTPConnection(p.hostname,p.port);c.request('POST',sys.argv[1],b'command-approved-body');print(c.getresponse().status)"
    args = ["-c", script, f"http://127.0.0.1:{port}/receive"]
    async def approve(action):
        approvals.append(action)
        return len(approvals) == 1
    async def reviewer(messages, _info):
        assert '127.0.0.1' in str(messages)
        yield json.dumps({"decision": "deny", "risk": "high", "authorization": "low", "rule": "general", "reason": "新命令捕获独立助理设置，具体副作用未获授权。", "same_denied_outcome": False})
    def update(event):
        if event.action is not None and event.action.kind == "command" and event.action.status == "started":
            conversation.set_turn_reviewer(task.id, "assistant")
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) < 2:
            yield {0: DeltaToolCall(name="run_command", json_args=json.dumps({"program": sys.executable, "arguments": args}), tool_call_id=f"network-command-{len(results)}")}
        else:
            yield "逐次联网决定完成。"
    async def run():
        turn = await conversation.ask(task.goal, document=None, task=task, scope=task.scope, model=FunctionModel(stream_function=model), review_model=FunctionModel(stream_function=reviewer), approve_action=approve, on_update=update)
        assert turn.status == "completed", turn.error
        assert received == [b"command-approved-body"]
        assert len(approvals) == 1
        reviews = [action for action in turn.actions if action.kind == "network"]
        assert reviews[1].review.status == "denied" and reviews[1].approval_settings.reviewer == "assistant"
        assert [action.approval_settings.reviewer for action in reviews] == ["user", "assistant"]
        for action in reviews:

            assert action.prepared_parameters["protocol"] == "http"
            assert action.prepared_parameters["host"] == "127.0.0.1"
            assert action.prepared_parameters["port"] == port
            assert action.prepared_parameters["command"]["arguments"] == args
        commands = [action for action in turn.actions if action.kind == "command"]
        assert [action.stdout.strip() for action in commands] == ["204", "403"]
        assert all(action.sandbox_type == "windowsMxc" and action.cleanup_complete for action in commands)
        assert reviews[0].prepared_parameters["parent_action_id"] != reviews[1].prepared_parameters["parent_action_id"]
    asyncio.run(run())


def test_send_text_uses_native_network_and_preserves_exact_previously_read_content(tmp_path: Path, receiver) -> None:
    port, received = receiver
    source = tmp_path / "notes.txt"
    source.write_bytes(b"exact-approved-send-body")
    approvals = []
    task = Task(goal="发送具体已读文字", scope=TaskScope(root=str(tmp_path), allowed_actions=[], provider="deepseek", recipients=["deepseek"]), request_limit=10,
                approval=ApprovalSettings(access=AccessSettings(network=NetworkSettings(allow_local_binding=True))))
    async def approve(action):
        approvals.append(action)
        assert action.kind == "send" and "exact-approved-send-body" in action.impact
        source.write_bytes(b"changed-after-reading-must-not-send")
        return True
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": "notes.txt"}), ("send_text", {"path": "notes.txt", "url": f"http://127.0.0.1:{port}/receive"})]
        if len(results) < len(steps):
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"send-{len(results)}")}
        else:
            yield "发送完成。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None, task=task, scope=task.scope, model=FunctionModel(stream_function=model), approve_action=approve)
        assert turn.status == "completed", turn.error
        assert len(approvals) == 1
        assert received == [b"exact-approved-send-body"]
        assert turn.actions[-1].status == "completed", turn.actions[-1].error
        assert turn.actions[-1].sandbox_type == "windowsMxc"
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["timeout", "disconnect", "explicit_deny"])
def test_native_network_never_sends_when_decision_is_missing_or_forbidden(tmp_path: Path, receiver, outcome: str) -> None:
    port, received = receiver
    async def run():
        waiting = asyncio.Event()
        async def decide(_request):
            waiting.set()
            await asyncio.Event().wait()
            return True
        executor = OfficialExecutor(tmp_path / "records" / "executor", network_decision=decide)
        network = NetworkSettings(denied_hosts=["127.0.0.1"] if outcome == "explicit_deny" else [], decision_timeout_seconds=.2 if outcome == "timeout" else 90)
        script = "import os,http.client,urllib.parse,sys;p=urllib.parse.urlsplit(os.environ['HTTP_PROXY']);c=http.client.HTTPConnection(p.hostname,p.port);c.request('POST',sys.argv[1],b'forbidden-body');print(c.getresponse().status)"
        try:
            sandbox = await executor.verify_scope(tmp_path, AccessSettings(writable_roots=[str(tmp_path)], network=network))
            running = asyncio.create_task(executor.run([sys.executable, "-c", script, f"http://127.0.0.1:{port}/receive"], tmp_path, sandbox=sandbox, network=network))
            if outcome == "disconnect":
                await asyncio.wait_for(waiting.wait(), 10)
                await executor.close()
                with pytest.raises(ExecutorError):
                    await running
            else:
                result = await running
                assert result["stdout"].strip() == "403", result
                assert waiting.is_set() is (outcome == "timeout")
            assert received == []
        finally:
            await executor.close()
    asyncio.run(run())


def test_full_access_sends_without_ordinary_approval_and_records_no_sandbox(tmp_path: Path, receiver) -> None:
    port, received = receiver
    (tmp_path / "notes.txt").write_bytes(b"full-mode-send-body")
    task = Task(goal="完全访问发送本机样例", scope=TaskScope(root=str(tmp_path), allowed_actions=[], provider="deepseek", recipients=["deepseek"]), request_limit=10,
                approval=ApprovalSettings(mode="full_access"))
    async def approve(_action):
        pytest.fail("Full access must not request ordinary network approval")
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": "notes.txt"}), ("send_text", {"path": "notes.txt", "url": f"http://127.0.0.1:{port}/receive"})]
        if len(results) < len(steps):
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"full-send-{len(results)}")}
        else:
            yield "发送完成。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None, task=task, scope=task.scope, model=FunctionModel(stream_function=model), approve_action=approve)
        assert turn.status == "completed", turn.error
        assert received == [b"full-mode-send-body"]
        assert all(action.status == "completed" and action.sandbox_type == "none" for action in turn.actions)
    asyncio.run(run())


def test_waiting_for_connection_approval_does_not_use_the_command_runtime_limit(tmp_path: Path, receiver) -> None:
    port, received = receiver
    task = Task(goal="等待本人决定后连接", scope=TaskScope(root=str(tmp_path), allowed_actions=[], provider="deepseek", recipients=["deepseek"]), request_limit=10)
    async def approve(_action):
        await asyncio.sleep(2)
        return True
    script = "import os,http.client,urllib.parse,sys;p=urllib.parse.urlsplit(os.environ['HTTP_PROXY']);c=http.client.HTTPConnection(p.hostname,p.port);c.request('POST',sys.argv[1],b'body-after-user-wait');print(c.getresponse().status)"
    async def model(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="run_command", json_args=json.dumps({"program": sys.executable, "arguments": ["-c", script, f"http://127.0.0.1:{port}/receive"], "timeout_seconds": 1.5}), tool_call_id="waiting-network-command")}
        else:
            yield "等待不计入程序运行时限。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None, task=task, scope=task.scope, model=FunctionModel(stream_function=model), approve_action=approve)
        assert turn.status == "completed", turn.error
        command = next(action for action in turn.actions if action.kind == "command")
        assert command.status == "completed" and not command.timed_out, command.error
        assert received == [b"body-after-user-wait"]
        assert command.cleanup_complete is True
    asyncio.run(run())


def test_stopping_while_native_network_waits_finishes_the_owned_command(tmp_path: Path, receiver) -> None:
    from pydantic_ai import CancellationToken
    port, received = receiver
    token = CancellationToken()
    task = Task(goal="停止等待中的连接", scope=TaskScope(root=str(tmp_path), allowed_actions=[], provider="deepseek", recipients=["deepseek"]), request_limit=10)
    script = "import os,http.client,urllib.parse,sys;p=urllib.parse.urlsplit(os.environ['HTTP_PROXY']);c=http.client.HTTPConnection(p.hostname,p.port);c.request('POST',sys.argv[1],b'body-must-not-send');print(c.getresponse().status)"
    async def run():
        waiting, release = asyncio.Event(), asyncio.Event()
        async def approve(_action):
            waiting.set()
            await release.wait()
            return True
        async def model(messages, _info):
            if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
                yield {0: DeltaToolCall(name="run_command", json_args=json.dumps({"program": sys.executable, "arguments": ["-c", script, f"http://127.0.0.1:{port}/receive"]}), tool_call_id="stop-network-command")}
            else:
                yield "不得继续发送。"
        running = asyncio.create_task(Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None, task=task, scope=task.scope, model=FunctionModel(stream_function=model), approve_action=approve, cancellation_token=token))
        await asyncio.wait_for(waiting.wait(), 10)
        token.cancel()
        turn = await asyncio.wait_for(running, 10)
        release.set()
        assert turn.status == "cancelled"
        command = next(action for action in turn.actions if action.kind == "command")
        assert command.status == "cancelled" and command.cleanup_complete is True
        assert received == []
    asyncio.run(run())


def test_full_access_preselected_check_can_use_its_exact_outside_working_folder(tmp_path: Path) -> None:
    folder, outside = tmp_path / "task", tmp_path / "outside"
    folder.mkdir()
    outside.mkdir()
    task = Task(goal="在指定外部位置检查", scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"],
        checks=[{"name": "外部检查", "argv": [sys.executable, "-c", "from pathlib import Path;Path('result.txt').write_text('exact-outside-check');print('outside-check-output')"], "cwd": str(outside)}]),
        request_limit=10, approval=ApprovalSettings(mode="full_access"))
    async def model(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="run_check", json_args='{"name":"外部检查"}', tool_call_id="full-outside-check")}
        else:
            yield "指定检查完成。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None, task=task, scope=task.scope, model=FunctionModel(stream_function=model))
        assert turn.status == "completed", turn.error
        assert turn.actions[-1].status == "completed", turn.actions[-1].error
        assert turn.actions[-1].sandbox_type == "none"
        assert (outside / "result.txt").read_text() == "exact-outside-check"
    asyncio.run(run())
