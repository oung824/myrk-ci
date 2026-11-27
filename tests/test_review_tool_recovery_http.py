import asyncio
import json
from pathlib import Path

import httpx2
from openai import AsyncOpenAI
import pytest
from pydantic_ai import models
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from myrk.actions import Action, approval_context
from myrk.conversation import Task, TaskScope
from myrk.permissions import ApprovalSettings
from myrk.review import review_action
from myrk.task_control import TaskBudget


def test_review_failure_result_reaches_the_next_actual_sdk_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    folder = tmp_path / "materials"
    folder.mkdir()
    target = tmp_path / "candidate.txt"
    task = Task(goal="仅审核明确指定的小文件，候选动作尚未执行", scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]),
                approval=ApprovalSettings(mode="auto_review"))
    action = Action(task_id=task.id, kind="create", target=str(target), approval_settings=task.approval,
                    prepared_parameters={"content": "synthetic candidate"})
    requests: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert len(requests) <= 2
        if len(requests) == 1:
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "missing-inspection", "type": "function",
                "function": {"name": "read_text", "arguments": '{"path":"missing.txt"}'}}]}
            finish = "tool_calls"
        else:
            tool_results = [message for message in payload["messages"] if message["role"] == "tool"]
            assert len(tool_results) == 1
            assert tool_results[0]["tool_call_id"] == "missing-inspection"
            assert "未取得本次证据" in tool_results[0]["content"]
            assert "ExecutorRequestError" in tool_results[0]["content"]
            decision = {"decision": "allow", "risk": "low", "authorization": "high", "rule": "explicit_allow",
                "reason": "用户明确授权确切候选文件及内容，缺失读取已如实记录，审核没有执行候选动作。", "same_denied_outcome": False}
            delta = {"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)}
            finish = "stop"
        chunks = [
            {"id": f"review-{len(requests)}", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": f"review-{len(requests)}", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
             "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
             "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}},
        ]
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n")

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client))
                return await review_action(action, model=model, budget=TaskBudget(task), directory=tmp_path / "review",
                    messages=[], denials=[], save=lambda: None, context=lambda: approval_context(task))

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    assert asyncio.run(run())
    assert action.review.status == "allowed" and action.review.attempts == task.review_requests == 2
    assert len(requests) == task.requests == task.total_requests == 2
    assert not target.exists()
