import asyncio
import json
from pathlib import Path

import httpx2
from openai import AsyncOpenAI
import pytest
from pydantic_ai import models
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from myrk.actions import Action, ActionExecutor, ToolOrigin, approval_context, tool_origin
from myrk.conversation import Task
from myrk.permissions import ApprovalSettings
from myrk.review import review_action
from myrk.task_control import TaskBudget


def test_independent_review_receives_extension_origin_and_only_readonly_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict] = []
    actions: list[Action] = []
    task = Task(goal="审核扩展工具的确切文件任务", scope=None, request_limit=10,
                approval=ApprovalSettings(mode="auto_review"))
    budget = TaskBudget(task)
    target = tmp_path / "approved.txt"

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        decision = json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general",
                               "reason": "用户明确授权确切文件和内容，来源及影响已经核实。", "same_denied_outcome": False})
        chunks = [{"id": "review-response", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": decision}, "finish_reason": None}]},
                  {"id": "review-response", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        return httpx2.Response(200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n",
                               headers={"content-type": "text/event-stream"})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client))

                async def review(action: Action) -> bool:
                    return await review_action(action, model=model, budget=budget, directory=tmp_path / "review",
                                               messages=[], denials=[], save=lambda: None, context=lambda: approval_context(task))

                async def operation(action: Action) -> str:
                    target.write_text("已审核的扩展内容", encoding="utf-8")
                    return "created"

                executor = ActionExecutor(actions=actions, budget=budget, save=lambda: None, notify=lambda action: None, review=review)
                token = tool_origin.set(ToolOrigin(name="fixture_create_report", arguments={"path": str(target), "content": "已审核的扩展内容"},
                                                   source="extension", schema_version="fixture-schema-v1"))
                try:
                    await executor.run("create", str(target), "创建确切文件", operation, requires_approval=True,
                                       parameters={"content": "已审核的扩展内容"})
                finally:
                    tool_origin.reset(token)

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())

    assert target.read_text(encoding="utf-8") == "已审核的扩展内容"
    assert actions[0].approval_basis == "assistant"
    assert len(requests) == 1
    materials = json.loads(next(message["content"] for message in requests[0]["messages"] if message["role"] == "user"))
    assert materials["action"]["tool_name"] == "fixture_create_report"
    assert materials["action"]["tool_arguments"] == {"path": str(target), "content": "已审核的扩展内容"}
    assert materials["action"]["tool_source"] == "extension"
    assert materials["action"]["tool_schema_version"] == "fixture-schema-v1"
    assert {tool["function"]["name"] for tool in requests[0]["tools"]} == {"read_text", "run_command"}


def test_independent_review_refreshes_extension_origin_with_changed_authorization(tmp_path: Path) -> None:
    task = Task(goal="核对原始授权", scope=None, request_limit=10, approval=ApprovalSettings(mode="auto_review"))
    action = Action(task_id=task.id, kind="create", target="report.txt", approval_settings=task.approval,
                    tool_name="fixture_create_report", tool_arguments={"content": "原始内容"},
                    tool_source="extension", tool_schema_version="fixture-schema-v1")
    materials: list[dict] = []

    async def respond(messages, _info):
        prompt = next(part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart))
        materials.append(json.loads(prompt))
        if len(materials) == 1:
            task.goal = "用户已更新确切文件任务"
            action.tool_name = "fixture_create_updated_report"
            action.tool_arguments = {"content": "更新后的内容"}
            action.tool_source = "updated-extension"
            action.tool_schema_version = "fixture-schema-v2"
        yield json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general",
                          "reason": "已按本次提供的确切文件内容及有效授权完成核对。", "same_denied_outcome": False})

    allowed = asyncio.run(review_action(action, model=FunctionModel(stream_function=respond), budget=TaskBudget(task),
                                       directory=tmp_path / "review", messages=[], denials=[], save=lambda: None,
                                       context=lambda: approval_context(task)))

    assert allowed
    assert len(materials) == 2
    assert materials[1]["goal"] == "用户已更新确切文件任务"
    assert materials[1]["action"]["tool_name"] == "fixture_create_updated_report"
    assert materials[1]["action"]["tool_arguments"] == {"content": "更新后的内容"}
    assert materials[1]["action"]["tool_source"] == "updated-extension"
    assert materials[1]["action"]["tool_schema_version"] == "fixture-schema-v2"


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("field, changed", [("tool_name", "unreviewed_tool"),
                                           ("tool_arguments", {"content": "未审核内容"}),
                                           ("tool_source", "unreviewed_source"),
                                           ("tool_schema_version", "unreviewed-schema")])
def test_changed_extension_origin_invalidates_approved_action(tmp_path: Path, field: str, changed: object, prepared: bool) -> None:
    actions: list[Action] = []
    task = Task(goal="仅批准原始扩展来源的确切动作", scope=None, request_limit=10,
                approval=ApprovalSettings(mode="auto_review"))
    budget = TaskBudget(task)
    target = tmp_path / "must-not-exist.txt"

    async def review(action: Action) -> bool:
        async def respond(_messages, _info):
            setattr(action, field, changed)
            yield json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general",
                              "reason": "审核仅允许本次提供的确切来源、参数和文件内容。", "same_denied_outcome": False})

        return await review_action(action, model=FunctionModel(stream_function=respond), budget=budget,
                                   directory=tmp_path / "review", messages=[], denials=[], save=lambda: None,
                                   context=lambda: approval_context(task))

    async def prepare(action: Action) -> None:
        action.prepared_parameters = {"content": "原始内容", "prepared": True}

    async def operation(action: Action) -> str:
        target.write_text("must-not-execute", encoding="utf-8")
        return "unexpected execution"

    async def run() -> None:
        executor = ActionExecutor(actions=actions, budget=budget, save=lambda: None, notify=lambda action: None, review=review)
        token = tool_origin.set(ToolOrigin(name="fixture_create_report", arguments={"content": "原始内容"},
                                           source="extension", schema_version="fixture-schema-v1"))
        try:
            await executor.run("create", str(target), "创建已审核的确切文件", operation, requires_approval=True,
                               parameters={"content": "原始内容"}, prepare_operation=prepare if prepared else None)
        finally:
            tool_origin.reset(token)

    asyncio.run(run())

    assert not target.exists()
    assert actions[0].status == "denied" and actions[0].effect == "not_started"
    assert actions[0].review is not None and actions[0].review.status == "context_changed"
    assert "旧允许已失效" in actions[0].error
