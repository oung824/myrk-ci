import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path

import pytest
from pydantic_ai import CancellationToken, models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.wrapper import WrapperModel

from myrk.conversation import Conversation, Supplement, Task, TaskScope
from myrk.permissions import ApprovalSettings
from myrk.executor import POWERSHELL


def allowed_review() -> str:
    return json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general",
                       "reason": "用户明确允许这个具体位置的小文件及其内容，影响已经核实。", "same_denied_outcome": False})


def test_stopping_nested_sdk_review_closes_streams_without_asyncio_errors(tmp_path: Path, caplog, monkeypatch):
    from tests.support.agent_acceptance.replay import OfflineSession, Reply

    caplog.set_level(logging.ERROR, logger="asyncio")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)

    async def run():
        folder = tmp_path / "materials"
        folder.mkdir()
        (folder / "proof.txt").write_text("synthetic independent evidence", encoding="utf-8")
        target = tmp_path / "unwritten.txt"
        session = OfflineSession("deepseek", [
            Reply(tool="create_text", arguments={"path": str(target), "content": "must-not-write"}),
            Reply(tool="read_text", arguments={"path": "proof.txt"}),
        ])
        token = CancellationToken()
        review_open = asyncio.Event()
        streams = 0
        scope = TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"])
        task = Task(goal="Stop the pending independently reviewed creation", scope=scope,
                    approval=ApprovalSettings(mode="auto_review"), provider_config=session.config)

        class PassthroughModel(WrapperModel):
            @asynccontextmanager
            async def request_stream(self, *args, **kwargs):
                nonlocal streams
                streams += 1
                async with self.wrapped.request_stream(*args, **kwargs) as response:
                    if streams == 2:
                        review_open.set()
                    yield response

        async def stop_during_review():
            await review_open.wait()
            token.cancel()

        async with session.model() as captured:
            model = PassthroughModel(captured.wrapped)
            stopper = asyncio.create_task(stop_during_review())
            try:
                async with asyncio.timeout(10):
                    turn = await Conversation(tmp_path / "session.json").ask(task.goal, document=None,
                        scope=scope, task=task, model=model, review_model=model, cancellation_token=token)
            finally:
                stopper.cancel()
                await asyncio.gather(stopper, return_exceptions=True)
        await asyncio.sleep(.02)
        assert token.cancelled, (turn.status, turn.error, len(session.exchanges))
        assert len(session.exchanges) == task.requests == 2
        assert turn.status == "cancelled"
        assert not target.exists()
        action = turn.actions[0]
        assert action.status == "cancelled" and action.review.status == "cancelled"
        assert task.review_requests == len(action.review.request_attempts) == 1

    asyncio.run(run())
    errors = [record for record in caplog.records if record.name == "asyncio" and record.levelno >= logging.ERROR]
    assert not errors, [record.getMessage() for record in errors]


@pytest.mark.parametrize("first_response,review_turns", [(503, 1), ("invalid", 2)])
def test_review_counts_new_judgments_but_keeps_connection_retry_in_same_turn(tmp_path: Path, first_response: int | str, review_turns: int):
    folder = tmp_path / "materials"
    folder.mkdir()
    target = tmp_path / "approved.txt"
    scope = TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="创建明确指定的小文件", scope=scope, approval=ApprovalSettings(mode="auto_review"))
    responses = iter([first_response, "allow"])

    async def main(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": "approved"}), tool_call_id="create")}
        else:
            yield "已完成，等待验收。"

    async def reviewer(_messages, _info):
        response = next(responses)
        if isinstance(response, int):
            raise ModelHTTPError(response, "offline-review")
        yield "安全" if response == "invalid" else allowed_review()

    path = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(path).ask(task.goal, document=None, scope=scope, task=task,
                       model=FunctionModel(stream_function=main), review_model=FunctionModel(stream_function=reviewer)))
    assert turn.status == "completed", turn.error
    assert target.read_text(encoding="utf-8") == "approved"
    assert task.review_requests == 2
    assert task.requests == task.total_requests == 4
    assert task.turns_used == 2 + review_turns
    review = turn.actions[-1].review
    assert [item.number for item in review.request_attempts] == [2, 3]
    assert [item.own_number for item in review.request_attempts] == [2, 3]
    assert [item.actor for item in review.request_attempts] == ["action_review", "action_review"]
    assert all(item.delegation_id is None for item in review.request_attempts)
    assert review.request_owner_id is None
    assert [item.number for item in turn.request_attempts] == [1, 4]
    assert [item.status for item in review.request_attempts] == (["failed", "completed"] if first_response == 503 else ["completed", "completed"])
    if first_response == 503:
        assert review.request_attempts[0].http_status == 503
        assert "ModelHTTPError" in review.request_attempts[0].error
        assert review.request_attempts[0].input_tokens is None
    assert review.request_attempts[-1].model
    assert review.request_attempts[-1].provider
    assert review.request_attempts[-1].input_tokens is not None
    assert Conversation.open(path).turns[-1].actions[-1].review == review


@pytest.mark.parametrize("capacity_fallback", [False, True])
@pytest.mark.parametrize("decision", ["deny", "missing", "error", "stop"])
def test_human_denial_requires_an_actual_decision_before_stop(tmp_path: Path, capacity_fallback: bool, decision: str):
    folder = tmp_path / "materials"
    folder.mkdir()
    target = tmp_path / "unwritten.txt"
    scope = TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="核对具体动作的本人决定", scope=scope,
                approval=ApprovalSettings(mode="auto_review" if capacity_fallback else "request_approval"))
    token = CancellationToken()
    called = []

    async def human(action):
        called.append(action.id)
        if decision == "error":
            raise OSError("本人决定入口未完成")
        if decision == "stop":
            token.cancel()
        return False

    async def main(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            content = "x" * 600_000 if capacity_fallback else "unwritten"
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": content}), tool_call_id="create")}
        else:
            yield "本轮结束，动作尚未执行。"

    async def reviewer(_messages, _info):
        raise AssertionError("容量不足的必要材料不能发送给审核服务")
        yield ""

    path = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(path).ask(task.goal, document=None, scope=scope, task=task, cancellation_token=token,
                       model=FunctionModel(stream_function=main), review_model=FunctionModel(stream_function=reviewer),
                       approve_action=None if decision == "missing" else human))
    action = turn.actions[-1]
    assert bool(called) == (decision != "missing")
    assert not target.exists()
    assert task.review_requests == 0
    assert action.status == ("cancelled" if decision == "stop" else "failed" if decision == "error" else "denied")
    assert action.denial_source == ("user" if decision == "deny" else "review_incomplete" if capacity_fallback else None)
    assert f"本人明确拒绝：{1 if decision == 'deny' else 0}" in turn.execution_facts
    if decision == "deny":
        assert "用户未批准" in action.error
    assert Conversation.open(path).turns[-1].actions[-1].denial_source == action.denial_source


def test_completed_readonly_review_then_connection_retry_keeps_new_turn_and_attempt_facts(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "proof.txt").write_text("必要的核对资料", encoding="utf-8")
    target = tmp_path / "approved.txt"
    scope = TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="核对资料后创建指定小文件", scope=scope, approval=ApprovalSettings(mode="auto_review"))
    calls = 0

    async def main(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": "approved"}), tool_call_id="create")}
        else:
            yield "已完成，等待验收。"

    async def reviewer(messages, _info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"proof.txt"}', tool_call_id="review-read")}
        elif calls == 2:
            assert "必要的核对资料" in str(messages)
            raise ModelHTTPError(503, "offline-review", body={"message": "never-store-this-error-body"})
        else:
            yield allowed_review()

    path = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(path).ask(task.goal, document=None, scope=scope, task=task,
                       model=FunctionModel(stream_function=main), review_model=FunctionModel(stream_function=reviewer)))
    assert turn.status == "completed", turn.error
    assert target.read_text(encoding="utf-8") == "approved"
    assert task.review_requests == 3
    assert task.requests == task.total_requests == 5
    assert task.turns_used == 4
    attempts = turn.actions[-1].review.request_attempts
    assert [item.number for item in attempts] == [2, 3, 4]
    assert [item.status for item in attempts] == ["completed", "failed", "completed"]
    assert attempts[1].http_status == 503
    assert "never-store-this-error-body" not in path.read_text(encoding="utf-8")
    assert Conversation.open(path).turns[-1].actions[-1].review.request_attempts == attempts


def test_changed_authorization_cannot_start_new_child_review_after_child_turn_limit(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "notes.txt").write_text("原始资料", encoding="utf-8")
    target = folder / ".git" / "unwritten.txt"
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="分工创建确切文件后主助理继续核验", scope=scope, delegation_max_turns=2, approval=ApprovalSettings(mode="auto_review"))
    token = CancellationToken()
    calls = {"main": 0, "child": 0, "review": 0}

    async def respond(messages, _info):
        child = any(isinstance(part, UserPromptPart) and str(part.content).startswith("受委派工作：") for message in messages for part in message.parts)
        role = "child" if child else "main"
        calls[role] += 1
        if child:
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": "unwritten"}), tool_call_id="child-create")}
        elif calls[role] == 1:
            yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": "创建确切文件", "context": str(target), "evidence_requirements": "真实动作", "expected_files": ["notes.txt"]}), tool_call_id="delegate")}
        elif calls[role] == 2:
            assert "先暂停这个原动作" in str(messages)
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id="main-read")}
        else:
            assert "原始资料" in str(messages)
            yield "主助理仍已实际读回资料，分工未完成部分保留。"

    async def reviewer(_messages, _info):
        calls["review"] += 1
        task.supplements.append(Supplement(text="先暂停这个原动作，按新要求重新准备。"))
        yield allowed_review()

    path = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(path).ask(task.goal, document=None, scope=scope, task=task, cancellation_token=token,
                       model=FunctionModel(stream_function=respond), review_model=FunctionModel(stream_function=reviewer)))
    assert turn.status == "completed", turn.error
    assert calls == {"main": 3, "child": 1, "review": 1}
    assert not token.cancelled
    assert not target.exists()
    assert task.turns_used == task.requests == 3
    assert task.total_requests == 5
    work = turn.delegations[0]
    assert work.status == "limited"
    assert work.turns_used == work.requests == work.max_turns == 2
    assert work.review_requests == 1
    child = Conversation.open(Path(work.record_path)).turns[-1]
    review = child.actions[-1].review
    assert review.status == "failed"
    assert review.request_owner_id == work.id
    assert [item.number for item in review.request_attempts] == [3]
    assert [item.own_number for item in review.request_attempts] == [2]
    assert review.request_attempts[0].delegation_id == work.id
    assert review.request_attempts[0].status == "completed"
    assert Conversation.open(path).turns[-1].task.total_requests == 5


def test_main_and_child_review_attempts_keep_unique_sequence_and_survive_later_main_error(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "notes.txt").write_text("可回查资料", encoding="utf-8")
    child_target = folder / ".git" / "child.txt"
    main_target = tmp_path / "main.txt"
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="主子助理各创建一个确切文件", scope=scope, delegation_max_turns=5, approval=ApprovalSettings(mode="auto_review"))
    calls = {"main": 0, "child": 0, "review": 0}

    async def respond(messages, _info):
        child = any(isinstance(part, UserPromptPart) and str(part.content).startswith("受委派工作：") for message in messages for part in message.parts)
        role = "child" if child else "main"
        calls[role] += 1
        if child and calls[role] == 1:
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(child_target), "content": "child-approved"}), tool_call_id="child-create")}
        elif child:
            yield "分工已创建指定文件，等待主助理核验。"
        elif calls[role] == 1:
            yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": "创建指定文件", "context": str(child_target), "evidence_requirements": "真实动作", "expected_files": ["notes.txt"]}), tool_call_id="delegate")}
        elif calls[role] == 2:
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(main_target), "content": "main-approved"}), tool_call_id="main-create")}
        elif calls[role] == 3:
            raise ModelHTTPError(503, "offline-main", body={"message": "never-store-main-error-body"})
        else:
            yield "主助理指定文件已创建，分工结果仍等待核验。"

    async def reviewer(_messages, _info):
        calls["review"] += 1
        if calls["review"] == 1:
            raise ModelHTTPError(503, "offline-child-review", body={"message": "never-store-child-error-body"})
        yield allowed_review()

    path = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(path).ask(task.goal, document=None, scope=scope, task=task,
                       model=FunctionModel(stream_function=respond), review_model=FunctionModel(stream_function=reviewer)))
    assert turn.status == "completed", turn.error
    assert calls == {"main": 4, "child": 2, "review": 3}
    assert main_target.read_text(encoding="utf-8") == "main-approved"
    assert child_target.read_text(encoding="utf-8") == "child-approved"
    work = turn.delegations[0]
    child = Conversation.open(Path(work.record_path)).turns[-1]
    child_review = child.actions[-1].review
    main_review = next(action.review for action in turn.actions if action.kind == "create" and action.delegation_id is None)
    assert main_review.request_owner_id is None
    assert [item.number for item in main_review.request_attempts] == [7]
    assert [item.own_number for item in main_review.request_attempts] == [3]
    assert [item.status for item in main_review.request_attempts] == ["completed"]
    assert child_review.request_owner_id == work.id
    assert [item.number for item in child_review.request_attempts] == [3, 4]
    assert [item.own_number for item in child_review.request_attempts] == [2, 3]
    assert all(item.delegation_id == work.id for item in child_review.request_attempts)
    assert [item.status for item in child_review.request_attempts] == ["failed", "completed"]
    assert child_review.request_attempts[0].http_status == 503
    assert [item.number for item in turn.request_attempts] == [1, 6, 8, 9]
    assert [item.own_number for item in turn.request_attempts] == [1, 2, 4, 5]
    assert [item.status for item in turn.request_attempts] == ["completed", "completed", "failed", "completed"]
    assert turn.request_attempts[-2].http_status == 503
    all_attempts = [*turn.request_attempts, *child.request_attempts, *main_review.request_attempts, *child_review.request_attempts]
    assert sorted(item.number for item in all_attempts) == list(range(1, 10))
    assert task.requests == 5 and task.turns_used == 4 and task.review_requests == 1
    assert work.requests == 4 and work.turns_used == 3 and work.review_requests == 2
    assert task.total_requests == 9
    assert "never-store" not in path.read_text(encoding="utf-8")
    assert "never-store" not in Path(work.record_path).read_text(encoding="utf-8")
    saved = Conversation.open(path).turns[-1]
    assert saved.task.total_requests == 9
    assert next(action.review for action in saved.actions if action.kind == "create" and action.delegation_id is None) == main_review
    assert next(action.review for action in saved.actions if action.kind == "create" and action.delegation_id == work.id) == child_review


@pytest.mark.parametrize("forced_tool", ["create_text", "run_command"])
def test_child_final_turn_cannot_execute_registered_tool_even_if_model_forces_it(tmp_path: Path, forced_tool: str):
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "notes.txt").write_text("原始核对资料", encoding="utf-8")
    target = folder / "must-not-start.txt"
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit", "check"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="子助理最后一轮只文字交回，主助理继续", scope=scope, delegation_max_turns=2,
                approval=ApprovalSettings(mode="full_access"))
    token = CancellationToken()
    calls = {"main": 0, "child": 0}

    async def respond(messages, info):
        child = any(isinstance(part, UserPromptPart) and str(part.content).startswith("受委派工作：") for message in messages for part in message.parts)
        role = "child" if child else "main"
        calls[role] += 1
        if child and calls[role] == 1:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id="child-read")}
        elif child:
            assert info.function_tools == []
            parameters = {"path": str(target), "content": "last-turn-ran"} if forced_tool == "create_text" else {
                "program": POWERSHELL, "arguments": ["-NoProfile", "-Command", "[IO.File]::WriteAllText('" + str(target).replace("'", "''") + "','last-turn-ran')"]}
            yield {0: DeltaToolCall(name=forced_tool, json_args=json.dumps(parameters), tool_call_id="forced-last-turn")}
        elif calls[role] == 1:
            yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": "读资料后文字交回", "context": "只需读notes.txt", "evidence_requirements": "真实原文", "expected_files": ["notes.txt"]}), tool_call_id="delegate")}
        else:
            yield "主助理继续，已保留子助理结果和未完成部分。"

    path = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(path).ask(task.goal, document=None, scope=scope, task=task, cancellation_token=token,
                       model=FunctionModel(stream_function=respond)))
    assert turn.status == "completed", turn.error
    assert calls == {"main": 2, "child": 2}
    assert not token.cancelled
    assert not target.exists()
    work = turn.delegations[0]
    assert work.status == "limited"
    assert work.turns_used == work.requests == work.max_turns == 2
    assert task.requests == task.turns_used == 2 and task.total_requests == 4
    child = Conversation.open(Path(work.record_path)).turns[-1]
    assert not any(action.effect in {"completed", "unknown"} and action.kind in {"create", "command"} for action in child.actions)
    assert "最后一轮" in child.error


def test_review_using_final_child_turn_does_not_start_previously_proposed_action(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "notes.txt").write_text("继续核验的原始资料", encoding="utf-8")
    target = folder / ".git" / "unwritten.txt"
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="审核用掉最后一轮后不再启动普通动作", scope=scope, delegation_max_turns=2, approval=ApprovalSettings(mode="auto_review"))
    token = CancellationToken()
    calls = {"main": 0, "child": 0, "review": 0}

    async def respond(messages, _info):
        child = any(isinstance(part, UserPromptPart) and str(part.content).startswith("受委派工作：") for message in messages for part in message.parts)
        role = "child" if child else "main"
        calls[role] += 1
        if child:
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": "must-not-write"}), tool_call_id="child-create")}
        elif calls[role] == 1:
            yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": "创建确切文件", "context": str(target), "evidence_requirements": "真实动作", "expected_files": ["notes.txt"]}), tool_call_id="delegate")}
        elif calls[role] == 2:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id="main-read")}
        else:
            yield "主助理继续核验，已读回原始资料。"

    async def reviewer(_messages, _info):
        calls["review"] += 1
        yield allowed_review()

    path = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(path).ask(task.goal, document=None, scope=scope, task=task, cancellation_token=token,
                       model=FunctionModel(stream_function=respond), review_model=FunctionModel(stream_function=reviewer)))
    assert turn.status == "completed", turn.error
    assert calls == {"main": 3, "child": 1, "review": 1}
    assert not token.cancelled
    assert not target.exists()
    work = turn.delegations[0]
    assert work.status == "limited" and work.turns_used == work.requests == 2
    assert task.requests == task.turns_used == 3 and task.total_requests == 5
    child = Conversation.open(Path(work.record_path)).turns[-1]
    action = child.actions[-1]
    assert action.status == "cancelled" and action.effect == "not_started"
    assert action.review.status == "allowed"
    assert action.review.request_attempts[0].status == "completed"
    assert "最后一轮" in child.error


def test_review_final_turn_cannot_launch_registered_readonly_command(tmp_path: Path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    received = []
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "notes.txt").write_text("原始资料", encoding="utf-8")
    target = folder / ".git" / "unwritten.txt"
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="审核最后一轮只文字交回", scope=scope, delegation_max_turns=2, approval=ApprovalSettings(mode="auto_review"))
    task.approval.access.network.allowed_hosts = ["127.0.0.1"]
    token = CancellationToken()
    calls = {"main": 0, "child": 0, "review": 0}

    async def respond(messages, _info):
        child = any(isinstance(part, UserPromptPart) and str(part.content).startswith("受委派工作：") for message in messages for part in message.parts)
        role = "child" if child else "main"
        calls[role] += 1
        if child:
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": "unwritten"}), tool_call_id="child-create")}
        elif calls[role] == 1:
            yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": "创建确切文件", "context": str(target), "evidence_requirements": "真实动作", "expected_files": ["notes.txt"]}), tool_call_id="delegate")}
        else:
            yield "主助理继续，审核未完成部分保留。"

    async def reviewer(_messages, info):
        calls["review"] += 1
        assert info.function_tools == []
        script = f"Invoke-WebRequest -Uri 'http://127.0.0.1:{server.server_port}/' -Method Post -Body 'review-final-turn-started' -UseBasicParsing -TimeoutSec 3 | Out-Null"
        yield {0: DeltaToolCall(name="run_command", json_args=json.dumps({"program": POWERSHELL, "arguments": ["-NoProfile", "-Command", script]}), tool_call_id="forced-review-command")}

    path = tmp_path / "records" / "session.json"
    try:
        turn = asyncio.run(Conversation(path).ask(task.goal, document=None, scope=scope, task=task, cancellation_token=token,
                           model=FunctionModel(stream_function=respond), review_model=FunctionModel(stream_function=reviewer)))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert turn.status == "completed", turn.error
    assert calls == {"main": 2, "child": 1, "review": 1}
    assert not token.cancelled
    assert not target.exists()
    assert received == []
    work = turn.delegations[0]
    assert work.status == "limited" and work.turns_used == work.requests == 2
    child = Conversation.open(Path(work.record_path)).turns[-1]
    assert child.actions[-1].effect == "not_started"
    assert child.actions[-1].review.status == "failed"
    assert child.actions[-1].review.request_attempts[0].status == "completed"
    assert "最后一轮" in child.error


def test_local_turn_limit_does_not_cancel_another_command_that_already_started(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "notes.txt").write_text("已读的确切资料", encoding="utf-8")
    started = folder / "command-started.txt"
    finished = folder / "command-finished.txt"
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit", "check"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="本地到限只阻止新动作，已运行命令正常结束", scope=scope, delegation_max_turns=3, approval=ApprovalSettings(mode="auto_review"))
    token = CancellationToken()
    calls = {"main": 0, "child": 0, "review": 0}

    async def respond(messages, _info):
        child = any(isinstance(part, UserPromptPart) and str(part.content).startswith("受委派工作：") for message in messages for part in message.parts)
        role = "child" if child else "main"
        calls[role] += 1
        if child and calls[role] == 1:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id="child-read")}
        elif child:
            script = "[IO.File]::WriteAllText('" + str(started).replace("'", "''") + "','started'); Start-Sleep -Milliseconds 400; [IO.File]::WriteAllText('" + str(finished).replace("'", "''") + "','finished'); [Console]::WriteLine('kept-running')"
            yield {0: DeltaToolCall(name="run_command", json_args=json.dumps({"program": POWERSHELL, "arguments": ["-NoProfile", "-Command", script]}), tool_call_id="already-running"),
                   1: DeltaToolCall(name="send_text", json_args='{"path":"notes.txt","url":"http://127.0.0.1:12345/never-dispatch"}', tool_call_id="awaiting-review")}
        elif calls[role] == 1:
            yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": "核对两个动作的边界", "context": "命令允许在任务文件夹生成标记；外发仍需审核", "evidence_requirements": "真实动作", "expected_files": ["notes.txt"]}), tool_call_id="delegate")}
        else:
            yield "主助理继续，已运行命令应按实际结果交回。"

    async def reviewer(_messages, _info):
        calls["review"] += 1
        async with asyncio.timeout(10):
            while not started.exists():
                await asyncio.sleep(.01)
        yield allowed_review()

    path = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(path).ask(task.goal, document=None, scope=scope, task=task, cancellation_token=token,
                       model=FunctionModel(stream_function=respond), review_model=FunctionModel(stream_function=reviewer)))
    assert turn.status == "completed", turn.error
    assert calls == {"main": 2, "child": 2, "review": 1}
    assert not token.cancelled
    assert started.read_text(encoding="utf-8") == "started"
    assert finished.read_text(encoding="utf-8") == "finished"
    work = turn.delegations[0]
    assert work.status == "limited" and work.turns_used == work.requests == 3
    child = Conversation.open(Path(work.record_path)).turns[-1]
    command = next(action for action in child.actions if action.kind == "command")
    assert command.status == "completed" and command.effect == "completed"
    assert command.stdout.strip() == "kept-running" and command.cleanup_complete
    send = next(action for action in child.actions if action.kind == "send")
    assert send.effect == "not_started" and send.review.status == "allowed"
