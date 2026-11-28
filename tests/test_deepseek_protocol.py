import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import models
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from myrk.conversation import Conversation, TaskScope
from myrk.conversation import Task
from tests.support.observed_http import observed_mock_transport


@pytest.mark.parametrize("status,used,expected", [(503, 0, 3), (503, 9, 1), (400, 0, 1), (401, 0, 1), (403, 0, 1), (429, 0, 1)])
def test_service_failures_preserve_budget_and_manual_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, used: int, expected: int) -> None:
    requests = []
    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(status, json={"error": {"message": "controlled failure", **({"code": "insufficient_quota"} if status == 429 else {})}})
    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                path = tmp_path / "failed.json"
                task = Task(goal="保留这项工作", scope=None, request_limit=10, requests=used)
                turn = await Conversation(path).ask(task.goal, document=None, task=task, model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)))
                assert turn.service_failure and turn.status == "failed"
                assert turn.task.requests == used + expected and len(requests) == expected
                assert turn.input_tokens is None and turn.output_tokens is None
                opened = Conversation.open(path)
                assert opened.recovery is not None and opened.recovery.can_resume
                if used == 0:
                    from pydantic_ai.models.test import TestModel
                    resumed = await opened.resume_task(task.id, model=TestModel(call_tools=[], custom_output_text="由用户续办"))
                    assert resumed.status == "completed", resumed.error
                    assert resumed.task.id == task.id and resumed.task.requests == expected + 1
                else:
                    with pytest.raises(ValueError, match="上限"):
                        opened.prepare_resume(task.id)
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_temporary_service_failure_retries_inside_the_original_task_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []
    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if len(requests) < 3:
            return httpx2.Response(503, json={"error": {"message": "controlled temporary outage synthetic-only"}})
        chunk = {"id": "recovered", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "服务恢复后回答"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})
    async def run():
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                path = tmp_path / "retry.json"
                turn = await Conversation(path).ask("回答", document=None, model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)))
                assert turn.status == "completed", turn.error
                assert turn.task.requests == turn.requests == len(requests) == 3
                assert turn.input_tokens is None and turn.output_tokens is None
                record = json.loads(path.read_text(encoding="utf-8"))
                assert "synthetic-only" not in path.read_text(encoding="utf-8")
                assert [item["request_attempt"] for item in record["prepared_contexts"]] == [1, 2, 3]
                assert [item["http_status"] for item in record["turns"][0]["request_attempts"]] == [503, 503, None]
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_native_changed_endpoint_cannot_bypass_receiver_approval_on_manual_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.providers.openai import OpenAIProvider
    requests = []
    def respond(request):
        requests.append(request.url)
        return httpx2.Response(503, json={"error": {"message": "controlled"}})
    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                conversation = Conversation(tmp_path / "same-label.json")
                first = await conversation.ask("原工作", document=None, model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)))
            async with AsyncOpenAI(api_key="synthetic-only", base_url="http://127.0.0.1:9999/v1", http_client=http, max_retries=0) as client:
                with pytest.raises(ValueError, match="批准"):
                    await conversation.resume_task(first.task.id, model=OpenAIChatModel("second", provider=OpenAIProvider(openai_client=client)))
                assert len(requests) == 3
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_retry_adopts_a_received_supplement_before_preparing_its_actual_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from myrk.conversation import Supplement
    from myrk.task_control import TaskBudget
    requests = []
    task = Task(goal="原目标", scope=None, request_limit=10)
    budget = TaskBudget(task)
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(503, json={"error": {"message": "controlled"}})
    def observe(update):
        if update.kind == "status" and "暂时服务错误" in update.text and not task.supplements:
            task.supplements.append(Supplement(text="失败后到达的新补充必须在下一次发送采用"))
    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                path = tmp_path / "retry-supplement.json"
                await Conversation(path).ask(task.goal, document=None, task=task, budget=budget, on_update=observe, model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)))
                assert "失败后到达的新补充" in str(requests[1]["messages"])
                record = json.loads(path.read_text(encoding="utf-8"))
                assert "失败后到达的新补充" in str(record["prepared_contexts"][1]["messages"])
                assert task.supplements[0].applied_request == 2
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("child_usage", [True, False])
def test_delegation_native_messages_and_complete_or_unknown_total_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_usage: bool) -> None:
    (tmp_path / "notes.txt").write_text("真实原文", encoding="utf-8")
    requests: list[dict] = []
    def respond(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        messages = payload["messages"]
        child = any(message["role"] == "user" and message["content"].startswith("受委派工作：") for message in messages)
        results = [message for message in messages if message["role"] == "tool"]
        tool = None
        if not results:
            tool = ("read_text", {"path": "notes.txt"}) if child else ("delegate_work", {"goal": "读取资料", "context": "只读notes.txt", "evidence_requirements": "真实原文", "expected_files": ["notes.txt"]})
        elif not child and len(results) == 1:
            work = json.loads(results[0]["content"])
            tool = ("verify_delegation", {"delegation_id": work["id"], "path": "notes.txt", "expected_text": "真实原文"})
        delta = {"role": "assistant", "reasoning_content": "按实际证据办理。"}
        if tool:
            name, args = tool
            delta["tool_calls"] = [{"index": 0, "id": f"native-{len(requests)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
        else:
            delta["content"] = "真实原文已核对，待验收。"
        chunks = [{"id": f"response-{len(requests)}", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}, {"id": f"response-{len(requests)}", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]}]
        if not child or child_usage:
            chunks[-1]["usage"] = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        return httpx2.Response(200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
    async def run():
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-key-not-a-real-account", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                conversation = Conversation(tmp_path / "record.json")
                turn = await conversation.ask("读取后核验", document=None, model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)), thinking=True, scope=TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"]))
                assert turn.status == "completed", turn.error
                assert turn.task.requests == turn.requests == 3
                assert turn.delegations[0].requests == 2
                assert turn.task.total_requests == len(requests) == 5
                assert (turn.input_tokens, turn.output_tokens) == ((55, 35) if child_usage else (None, None))
                assert "synthetic-key-not-a-real-account" not in conversation.path.read_text(encoding="utf-8")
                parent = json.loads(conversation.path.read_text(encoding="utf-8"))
                child_record = json.loads(Path(turn.delegations[0].record_path).read_text(encoding="utf-8"))
                assert [item["request_attempt"] for item in parent["prepared_contexts"]] == [1, 4, 5]
                assert [item["request_attempt"] for item in child_record["prepared_contexts"]] == [2, 3]
                assert any(message["role"] == "tool" and "notes.txt:1" in message["content"] for message in requests[2]["messages"])
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("long_material", [False, True])
def test_deepseek_tool_and_reasoning_history_survives_reopening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, long_material: bool,
) -> None:
    requests: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            delta = {
                "role": "assistant",
                "reasoning_content": "先读取授权资料。",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "read-1",
                        "type": "function",
                        "function": {"name": "read_authorized_text", "arguments": "{}"},
                    }
                ],
            }
            reason = "tool_calls"
        else:
            delta = {"role": "assistant", "reasoning_content": "资料已核对。", "content": "800以上需审批。"}
            reason = "stop"
        chunks = [
            {
                "id": f"response-{len(requests)}",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": f"response-{len(requests)}",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx2.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def scenario() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(
                api_key="synthetic-key-not-a-real-account",
                base_url="https://api.deepseek.com",
                http_client=http,
                max_retries=0,
            ) as client:
                model = OpenAIChatModel(
                    "deepseek-flash", provider=DeepSeekProvider(openai_client=client)
                )
                path = tmp_path / "conversation.json"
                document = tmp_path / "规则.txt"
                document.write_text("800以上需审批。" + ("普通资料。\n" * 5000 + "结尾真实原句。" if long_material else ""), encoding="utf-8")
                first = await Conversation(path).ask(
                    "读取并说明。", document=document, model=model, thinking=True
                )
                second = await Conversation.open(path).ask(
                    "继续说明。", document=document, model=model, thinking=True
                )
                assert (first.status, second.status) == ("completed", "completed")
                assert (first.requests, first.input_tokens, first.output_tokens) == (2, 24, 6)
                assert "synthetic-key-not-a-real-account" not in path.read_text(encoding="utf-8")

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(scenario())

    resumed_messages = requests[2]["messages"]
    assert any(
        message.get("reasoning_content") == "先读取授权资料。"
        for message in resumed_messages
    )
    assert any(
        message.get("role") == "tool" and "800以上需审批。" in message["content"]
        for message in resumed_messages
    )
    if long_material:
        assert any(message.get("role") == "tool" and "全文证据编号：" in message["content"] and "结尾真实原句。" in message["content"] for message in resumed_messages)
        assert any(message.get("role") == "assistant" and message.get("tool_calls", [{}])[0].get("id") == "read-1" for message in resumed_messages)
