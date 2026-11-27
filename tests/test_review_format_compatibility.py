import asyncio
import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from myrk.actions import Action, approval_context
from myrk.conversation import Conversation, Task, TaskScope
from myrk.permissions import ApprovalSettings
from myrk.review import ReviewIncomplete, review_action
from myrk.task_control import TaskBudget


DECISION = {
    "decision": "allow", "risk": "low", "authorization": "high", "rule": "explicit_allow",
    "reason": "用户明确授权在确切位置新建本轮样例文件，内容和影响范围均已核对。",
    "same_denied_outcome": False,
}
JSON_DECISION = json.dumps(DECISION, ensure_ascii=False)


@pytest.fixture
def review_case(tmp_path: Path):
    task = Task(goal="在确切位置新建样例文件", scope=None, request_limit=10,
                approval=ApprovalSettings(mode="auto_review"))
    action = Action(task_id=task.id, kind="create", target=str(tmp_path / "sample.txt"),
                    approval_settings=task.approval, prepared_parameters={"content": "sample"})

    async def run(text: str) -> bool:
        async def respond(_messages, _info):
            yield text

        return await review_action(action, model=FunctionModel(stream_function=respond),
                                   budget=TaskBudget(task), directory=tmp_path / "review",
                                   messages=[], denials=[], save=lambda: None,
                                   context=lambda: "确切动作与授权未改变")

    return task, action, run


@pytest.mark.parametrize("text", [
    JSON_DECISION,
    "```json\n" + JSON_DECISION + "\n```",
    "```\n" + JSON_DECISION + "\n```",
    " \n```JSON \t\r\n" + JSON_DECISION + "\r\n```\n\t ",
])
def test_complete_review_object_accepts_only_its_single_outer_fence(review_case, text: str):
    task, action, run = review_case
    assert asyncio.run(run(text))
    assert action.review is not None
    assert action.review.status == "allowed"
    assert action.review.reason == DECISION["reason"]
    assert action.review.attempts == task.review_requests == task.requests == task.total_requests == 1
    assert len(action.review.request_attempts) == 1
    assert action.review.request_attempts[0].status == "completed"


@pytest.mark.parametrize("text", [
    "Explanation\n```json\n" + JSON_DECISION + "\n```",
    "```json\n" + JSON_DECISION + "\n```\nExplanation",
    "```json\n" + JSON_DECISION + "\n```\n```json\n" + JSON_DECISION + "\n```",
    "```json\n" + JSON_DECISION,
    "```json\n" + JSON_DECISION[:-1] + "\n```",
    "```json\n" + JSON_DECISION + "\n" + JSON_DECISION + "\n```",
    "```python\n" + JSON_DECISION + "\n```",
    "```json\n" + json.dumps({**DECISION, "reason": "嵌套标记 ``` 不属于完整单一外层代码块。"}) + "\n```",
    "```json\n" + json.dumps({**DECISION, "unexpected": True}) + "\n```",
    "```json\n" + json.dumps({**DECISION, "risk": "safe"}) + "\n```",
    "```json\n" + json.dumps({**DECISION, "reason": "short"}) + "\n```",
    "```json\n" + json.dumps({key: value for key, value in DECISION.items() if key != "risk"}) + "\n```",
])
def test_malformed_or_invalid_fenced_review_exhausts_its_existing_attempt_limit(review_case, text: str):
    task, action, run = review_case
    with pytest.raises(ReviewIncomplete):
        asyncio.run(run(text))
    assert action.review is not None
    assert action.review.status == "failed"
    assert action.review.attempts == task.review_requests == task.requests == task.total_requests == 3
    assert action.effect == "not_started"
    assert all(item.status == "completed" for item in action.review.request_attempts)


@pytest.mark.parametrize("changes", [
    {"decision": "deny"},
    {"risk": "critical"},
    {"risk": "high", "authorization": "low"},
    {"rule": "explicit_forbid"},
    {"rule": "sensitive_export", "authorization": "medium"},
    {"same_denied_outcome": True},
])
def test_valid_fenced_decision_keeps_existing_risk_and_authorization_rules(review_case, changes: dict):
    task, action, run = review_case
    text = "```json\n" + json.dumps({**DECISION, **changes}) + "\n```"
    assert not asyncio.run(run(text))
    assert action.review is not None
    assert action.review.status == "denied"
    assert action.review.attempts == task.review_requests == 1
    assert action.effect == "not_started"


def test_fenced_allow_creates_only_the_exact_approved_file_and_preserves_review_record(tmp_path: Path):
    folder = tmp_path / "materials"
    folder.mkdir()
    target = tmp_path / "approved.txt"
    task = Task(goal="在确切位置新建样例文件", request_limit=5,
                scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]),
                approval=ApprovalSettings(mode="auto_review"))

    async def respond(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(target), "content": "approved"}),
                                   tool_call_id="create")}
        else:
            yield "已完成，等待验收。"

    async def reviewer(_messages, _info):
        yield "```json\n" + JSON_DECISION + "\n```"

    path = tmp_path / "records" / "session.json"
    turn = asyncio.run(Conversation(path).ask(task.goal, document=None, scope=task.scope, task=task,
                       model=FunctionModel(stream_function=respond), review_model=FunctionModel(stream_function=reviewer)))
    assert turn.status == "completed", turn.error
    assert target.read_text(encoding="utf-8") == "approved"
    assert len(turn.actions) == 1
    action = turn.actions[0]
    assert action.status == action.effect == "completed"
    assert action.approval_basis == "assistant"
    assert action.review is not None and action.review.status == "allowed"
    assert action.review.attempts == task.review_requests == 1
    assert task.requests == task.total_requests == 3
    assert Conversation.open(path).turns[-1].actions[0] == action


def test_fenced_allow_is_not_applied_after_authorization_changes(tmp_path: Path):
    task = Task(goal="原来的确切授权", scope=None, request_limit=5, approval=ApprovalSettings(mode="auto_review"))
    action = Action(task_id=task.id, kind="create", target="sample.txt", approval_settings=task.approval)
    calls = 0

    async def reviewer(_messages, _info):
        nonlocal calls
        calls += 1
        if calls == 1:
            task.goal = "更新后不允许新建这个文件"
            yield "```json\n" + JSON_DECISION + "\n```"
        else:
            yield json.dumps({**DECISION, "decision": "deny", "rule": "explicit_forbid"})

    allowed = asyncio.run(review_action(action, model=FunctionModel(stream_function=reviewer), budget=TaskBudget(task),
                                       directory=tmp_path / "review", messages=[], denials=[], save=lambda: None,
                                       context=lambda: approval_context(task)))
    assert not allowed
    assert action.review is not None and action.review.status == "denied"
    assert action.effect == "not_started"
    assert calls == task.requests == task.review_requests == 2
