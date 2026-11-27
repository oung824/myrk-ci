import asyncio
import json
from pathlib import Path
import pytest

from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.exceptions import ModelHTTPError

from myrk.conversation import Conversation, Task, TaskScope
from myrk.permissions import ApprovalSettings


def test_parent_keeps_its_rounds_and_child_counter_survives_reopen(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "notes.txt").write_text("真实虚构成果", encoding="utf-8")
    scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="分工后主助理核验", scope=scope, delegation_max_turns=3)
    counts = {"main": 0, "child": 0}

    async def respond(messages, _info):
        child = any(isinstance(part, UserPromptPart) and str(part.content).startswith("受委派工作：") for message in messages for part in message.parts)
        role = "child" if child else "main"
        counts[role] += 1
        step = counts[role]
        if child:
            if step < 3:
                yield {0: DeltaToolCall(name="read_text", json_args=json.dumps({"path": "notes.txt"}), tool_call_id=f"child-{step}")}
            else:
                yield "虚构成果全文：真实虚构成果。"
        elif step == 1:
            yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": "读取成果", "context": "只读notes.txt", "evidence_requirements": "真实全文", "expected_files": ["notes.txt"]}), tool_call_id="delegate")}
        elif step == 2:
            work = json.loads(str(next(part.content for message in reversed(messages) for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "delegate_work")))
            yield {0: DeltaToolCall(name="verify_delegation", json_args=json.dumps({"delegation_id": work["id"], "path": "notes.txt", "expected_text": "真实虚构成果"}), tool_call_id="verify")}
        else:
            yield "真实文件已经核验，等待用户验收。"

    conversation = Conversation(tmp_path / "records" / "session.json")
    turn = asyncio.run(conversation.ask(task.goal, document=None, task=task, scope=scope, model=FunctionModel(stream_function=respond)))
    assert turn.status == "completed", turn.error
    assert counts == {"main": 3, "child": 3}
    assert task.requests == task.turns_used == 3
    assert task.total_requests == 6
    work = turn.delegations[0]
    assert work.requests == work.turns_used == work.max_turns == 3
    assert work.verification_status == "passed"
    child = Conversation.open(Path(work.record_path)).turns[-1]
    assert child.request_owner_id == work.id
    assert child.request_account is not None
    assert child.request_account.requests == child.request_account.turns_used == 3
    saved = Conversation.open(conversation.path).turns[-1]
    assert saved.task.total_requests == 6
    all_numbers = [item.number for item in turn.request_attempts] + [item.number for item in child.request_attempts]
    assert sorted(all_numbers) == [1, 2, 3, 4, 5, 6]
    assert [item.own_number for item in child.request_attempts] == [1, 2, 3]


def test_program_facts_count_actual_scope_denials_without_rewriting_model_answer(tmp_path: Path):
    folder = tmp_path / "materials"
    outside = tmp_path / "outside"
    folder.mkdir()
    outside.mkdir()
    scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    invalid_scope = scope.model_copy(update={"root": str(outside)})

    async def respond(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) < 4:
            yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": "越界分工", "context": "不应执行", "scope": invalid_scope.model_dump(), "expected_files": ["notes.txt"], "evidence_requirements": "原文"}), tool_call_id=f"denial-{len(results)}")}
        else:
            yield "总共发生5次独立审核拒绝。"

    turn = asyncio.run(Conversation(tmp_path / "session.json").ask("核对拒绝来源", document=None, scope=scope, model=FunctionModel(stream_function=respond)))
    assert turn.answer == "总共发生5次独立审核拒绝。"
    assert len({action.id for action in turn.actions if action.status == "denied"}) == 4
    assert "拒绝或未执行记录：4" in turn.execution_facts
    assert "规则或范围拒绝：4" in turn.execution_facts
    assert "独立审核明确拒绝：0" in turn.execution_facts


def test_network_retry_counts_real_attempts_without_spending_two_rounds(tmp_path: Path):
    attempts = 0

    async def respond(_messages, _info):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModelHTTPError(503, "offline-fixture")
        yield "已经收到回应。"

    turn = asyncio.run(Conversation(tmp_path / "session.json").ask("检查同一步的再次尝试", document=None, model=FunctionModel(stream_function=respond)))
    assert turn.status == "completed", turn.error
    assert turn.task.requests == turn.task.total_requests == 2
    assert turn.task.turns_used == 1
    assert [item.number for item in turn.request_attempts] == [1, 2]


def test_child_action_review_belongs_to_child_account(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "notes.txt").write_text("声明成果", encoding="utf-8")
    target = folder / ".git" / "fixture.txt"
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="分工创建明确允许的虚构配置文件", scope=scope, delegation_max_turns=5, approval=ApprovalSettings(mode="auto_review"))

    async def respond(messages, _info):
        child = any(isinstance(part, UserPromptPart) and str(part.content).startswith("受委派工作：") for message in messages for part in message.parts)
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if child and not results:
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": "approved-fixture"}), tool_call_id="child-create")}
        elif child:
            yield "已创建这份虚构配置，声明成果仍是notes.txt。"
        elif not results:
            yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": "创建明确指定的虚构配置", "context": f"用户允许创建{target}并只写approved-fixture", "evidence_requirements": "真实动作", "expected_files": ["notes.txt"]}), tool_call_id="delegate")}
        else:
            yield "收到分工结果，仍待核验。"

    async def review(_messages, _info):
        yield json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "用户明确允许在这个虚构材料位置新增本份小文件。", "same_denied_outcome": False})

    turn = asyncio.run(Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None, scope=scope, task=task,
                       model=FunctionModel(stream_function=respond), review_model=FunctionModel(stream_function=review)))
    assert turn.status == "completed", turn.error
    assert target.read_text(encoding="utf-8") == "approved-fixture"
    work = turn.delegations[0]
    assert task.requests == task.turns_used == 2
    assert task.review_requests == 0
    assert work.requests == work.turns_used == 3
    assert work.review_requests == 1
    assert task.total_requests == 5
    child = Conversation.open(Path(work.record_path)).turns[-1]
    assert child.request_account.review_requests == 1
    with pytest.raises(ValueError, match="子助理"):
        Conversation.open(Path(work.record_path)).prepare_resume(task.id)


def test_old_record_without_original_request_cap_is_rejected(tmp_path: Path):
    async def respond(_messages, _info):
        yield "原始记录。"

    path = tmp_path / "old.json"
    asyncio.run(Conversation(path).ask("旧任务", document=None, request_limit=2, model=FunctionModel(stream_function=respond)))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["version"] = 5
    old_task = record["turns"][0]["task"]
    old_task.pop("request_policy")
    old_task.pop("total_requests")
    old_task["request_limit"] = None
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="旧记录"):
        Conversation.open(path)
