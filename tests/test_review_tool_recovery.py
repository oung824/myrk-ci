import asyncio
import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from myrk.conversation import Conversation, Task, TaskScope
from myrk.executor import ExecutorError, ExecutorRequestError, OfficialExecutor
from myrk.permissions import AccessSettings, ApprovalSettings


@pytest.mark.parametrize(("first_tool", "first_arguments"), [
    ("read_text", {"path": "missing.txt"}),
    ("read_text", {"path": "non-utf8.txt"}),
    ("run_command", {"program": "myrk-missing-review-program", "arguments": []}),
])
def test_review_recovers_missing_evidence_with_another_readonly_check(tmp_path: Path, first_tool: str, first_arguments: dict):
    folder = tmp_path / "materials"
    folder.mkdir()
    proof = folder / "proof.txt"
    proof.write_text("用户已明确授权新增这份精确内容的小文件。", encoding="utf-8")
    (folder / "non-utf8.txt").write_bytes(b"\xff\xfe\xfa")
    target = tmp_path / "approved.txt"
    task = Task(goal="核对证据后新增指定的小文件", scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]),
                approval=ApprovalSettings(mode="auto_review"))

    async def main(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": "approved"}), tool_call_id="create")}
        else:
            yield "已按真实结果交付，等待用户验收。"

    async def reviewer(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name=first_tool, json_args=json.dumps(first_arguments), tool_call_id="missing-evidence")}
        elif len(results) == 1:
            assert results[0].tool_call_id == "missing-evidence"
            if first_tool == "run_command":
                assert results[0].outcome == "success"
                command_result = json.loads(str(results[0].content))
                assert command_result["exitCode"] != 0, command_result
                assert isinstance(command_result["stdout"], str) and command_result["stderr"], command_result
            else:
                assert results[0].outcome == "failed"
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"proof.txt"}', tool_call_id="actual-evidence")}
        else:
            assert results[-1].outcome == "success"
            assert "用户已明确授权" in str(results[-1].content)
            yield json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "explicit_allow",
                              "reason": "已取得真实证据，用户明确授权这个确切位置和内容的小文件。", "same_denied_outcome": False})

    record = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(record).ask(task.goal, document=None, scope=task.scope, task=task,
                      model=FunctionModel(stream_function=main), review_model=FunctionModel(stream_function=reviewer)))
    action = turn.actions[-1]
    assert action.status == "completed", action.error
    assert action.review.status == "allowed"
    assert target.read_text(encoding="utf-8") == "approved"
    assert proof.read_text(encoding="utf-8") == "用户已明确授权新增这份精确内容的小文件。"
    assert task.review_requests == action.review.attempts == 3
    assert task.requests == task.total_requests == 5
    assert [attempt.number for attempt in action.review.request_attempts] == [2, 3, 4]
    assert all(attempt.status == "completed" for attempt in action.review.request_attempts)
    failed_tools = [part for message in action.review.messages for part in message.parts
                    if isinstance(part, ToolReturnPart) and part.outcome == "failed"]
    assert [part.tool_call_id for part in failed_tools] == ([] if first_tool == "run_command" else ["missing-evidence"])
    if first_tool == "run_command":
        saved_command = next(part for message in action.review.messages for part in message.parts
                             if isinstance(part, ToolReturnPart) and part.tool_call_id == "missing-evidence")
        command_result = json.loads(str(saved_command.content))
        assert command_result["exitCode"] != 0 and command_result["stderr"], command_result
        assert isinstance(command_result["stdout"], str)
    assert Conversation.open(record).turns[-1].actions[-1].review == action.review


def test_failed_review_inspection_still_obeys_final_denial(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()
    target = tmp_path / "unwritten.txt"
    task = Task(goal="核对拒绝决定，候选文件不得创建", scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]),
                approval=ApprovalSettings(mode="auto_review"))

    async def main(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": "unwritten"}), tool_call_id="create")}
        else:
            yield "审核已拒绝，原动作未执行。"

    async def reviewer(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"missing.txt"}', tool_call_id="missing-evidence")}
        else:
            assert results[-1].outcome == "failed"
            yield json.dumps({"decision": "deny", "risk": "low", "authorization": "high", "rule": "explicit_forbid",
                              "reason": "用户明确禁止这个确切动作，读取失败不能改变原有禁止要求。", "same_denied_outcome": False})

    record = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(record).ask(task.goal, document=None, scope=task.scope, task=task,
                      model=FunctionModel(stream_function=main), review_model=FunctionModel(stream_function=reviewer)))
    action = turn.actions[-1]
    assert action.status == action.review.status == "denied"
    assert action.denial_source == "assistant" and not target.exists()
    assert task.review_requests == 2 and task.requests == task.total_requests == 4
    assert Conversation.open(record).turns[-1].actions[-1].review == action.review


def test_executor_distinguishes_explicit_request_failure_from_unavailable_execution(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()
    access = AccessSettings(read_only=True)

    async def run():
        unavailable = OfficialExecutor(tmp_path / "missing-executor", binary=tmp_path / "missing.exe")
        try:
            with pytest.raises(ExecutorError) as startup:
                await unavailable.verify_scope(folder, access)
            assert not isinstance(startup.value, ExecutorRequestError)
        finally:
            await unavailable.close()
        executor = OfficialExecutor(tmp_path / "records" / "executor")
        try:
            with pytest.raises(ExecutorRequestError):
                await executor.read_bytes(folder / "missing.txt", folder, access)
        finally:
            await executor.close()
        with pytest.raises(ExecutorError) as disconnected:
            await executor.request("fs/getMetadata", {"path": folder.as_uri(), "followSymlinks": False})
        assert not isinstance(disconnected.value, ExecutorRequestError)

    asyncio.run(run())


def test_unknown_executor_method_is_a_protocol_failure(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()

    async def run():
        executor = OfficialExecutor(tmp_path / "records" / "executor")
        try:
            await executor.verify_scope(folder, AccessSettings(read_only=True))
            with pytest.raises(ExecutorError) as unsupported:
                await executor.request("myrk/unsupported-review-method", {})
            assert not isinstance(unsupported.value, ExecutorRequestError), str(unsupported.value)
        finally:
            await executor.close()

    asyncio.run(run())


def test_three_failed_review_checks_stop_with_all_failure_results_saved(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()
    target = tmp_path / "unwritten.txt"
    task = Task(goal="审核未完成时不得新增文件", scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]),
                approval=ApprovalSettings(mode="auto_review"))

    async def main(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": "unwritten"}), tool_call_id="create")}
        else:
            yield "审核没有取得最终决定，原动作未执行。"

    async def reviewer(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        assert all(part.outcome == "failed" for part in results)
        yield {0: DeltaToolCall(name="read_text", json_args='{"path":"missing.txt"}', tool_call_id=f"missing-{len(results)}")}

    record = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(record).ask(task.goal, document=None, scope=task.scope, task=task,
                      model=FunctionModel(stream_function=main), review_model=FunctionModel(stream_function=reviewer)))
    action = turn.actions[-1]
    assert action.status == action.review.status == "failed"
    assert action.denial_source != "assistant" and not target.exists()
    assert task.review_requests == action.review.attempts == 3
    assert task.requests == task.total_requests == 5
    assert all(attempt.status == "completed" for attempt in action.review.request_attempts)
    failed_tools = [part for message in action.review.messages for part in message.parts
                    if isinstance(part, ToolReturnPart) and part.outcome == "failed"]
    assert [part.tool_call_id for part in failed_tools] == ["missing-0", "missing-1", "missing-2"]
    assert Conversation.open(record).turns[-1].actions[-1].review == action.review
