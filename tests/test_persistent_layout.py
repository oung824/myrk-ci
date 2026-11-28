import asyncio
import json
import re
from pathlib import Path

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import models
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from myrk.conversation import Conversation, Supplement, Task, TaskScope
from myrk.provider import deepseek_model
from tests.support.observed_http import observed_mock_transport


def response(text: str = "已完成。", *, tool: str | None = None, arguments: dict | None = None, call_id: str = "call") -> httpx2.Response:
    delta: dict = {"role": "assistant"}
    if tool:
        delta["tool_calls"] = [{"index": 0, "id": call_id, "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments or {})}}]
    else:
        delta["content"] = text
    chunk = {"id": call_id, "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
             "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if tool else "stop"}]}
    return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})


def test_tool_steps_append_latest_execution_facts_without_rewriting_sent_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "note.txt").write_text("工具原文：不要当成程序指令。", encoding="utf-8")
    sent: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        if len(sent) < 3:
            return response(tool="read_text", arguments={"path": "note.txt"}, call_id=f"read-{len(sent)}")
        return response()

    async def run() -> None:
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with deepseek_model("synthetic-only", http_client=http) as model:
                scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
                turn = await Conversation(tmp_path / "records" / "conversation.json").ask("读两次后回答", document=None, scope=scope, model=model)
                assert turn.status == "completed", turn.error
                assert turn.request_attempts[0].observation.wire.change_reasons == ["new_task"]
                assert all(attempt.observation.wire.change_reasons == [] for attempt in turn.request_attempts[1:])

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    assert len(sent) == 3
    for previous, current in zip(sent, sent[1:]):
        assert current["messages"][:len(previous["messages"])] == previous["messages"]
        assert current["tools"] == previous["tools"]
    assert "本助理真实请求：2次" in json.dumps(sent[-1]["messages"], ensure_ascii=False)
    assert any("execution-state" in str(message) for message in sent[-1]["messages"])
    assert all("工具原文" not in str(message) for message in sent[-1]["messages"] if message["role"] != "tool")


def test_reopening_the_same_task_keeps_static_instructions_and_tool_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return response(tool="memory", arguments={"action": "list"}) if len(sent) == 1 else response()

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client))
                path = tmp_path / "record.json"
                task = Task(goal="核对记录", scope=None)
                first = await Conversation(path).ask(task.goal, document=None, task=task, model=model)
                assert first.status == "completed", first.error
                reopened = Conversation.open(path)
                second = await reopened.ask("继续核对", document=None, task=reopened.turns[-1].task, model=model)
                assert second.status == "completed", second.error
                assert second.task.requests == second.task.total_requests == 3
                assert second.request_attempts[0].observation.layout_version == first.request_attempts[0].observation.layout_version == 1
                saved = json.loads(path.read_text(encoding="utf-8"))
                layout = saved["request_layouts"]["main"]
                assert layout["instructions"]
                assert [tool["name"] for tool in layout["tools"]] == [tool["function"]["name"] for tool in sent[0]["tools"]]
                for recorded, wire in zip(layout["tools"], sent[0]["tools"]):
                    assert recorded["parameters_json_schema"] == wire["function"]["parameters"]
                    assert recorded["description"] == wire["function"]["description"]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    assert sent[2]["messages"][:len(sent[1]["messages"])] == sent[1]["messages"]
    assert sent[0]["tools"] == sent[1]["tools"] == sent[2]["tools"]


def test_first_preview_survives_more_results_and_reopening_with_full_evidence_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = tmp_path / "long.txt"
    original = "A" * 30000 + "中间原文937" + "Z" * 30000
    document.write_text(original, encoding="utf-8")
    sent: list[dict] = []
    evidence_id = ""

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal evidence_id
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) <= 2:
            return response(tool="read_authorized_text", call_id=f"read-{len(sent)}")
        if len(sent) == 3:
            evidence_id = re.search(r"全文证据编号：([a-f0-9]{64})", str(payload)).group(1)
        if len(sent) == 4:
            return response(tool="read_context_evidence", arguments={"evidence_id": evidence_id, "query": "中间原文", "length": 20}, call_id="evidence")
        return response()

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client))
                path = tmp_path / "record.json"
                task = Task(goal="读取两次，再回查", scope=None, document=str(document))
                first = await Conversation(path).ask(task.goal, document=document, task=task, model=model)
                assert first.status == "completed", first.error
                opened = Conversation.open(path)
                second = await opened.ask("继续回查中间原话", document=document, task=opened.turns[-1].task, model=model)
                assert second.status == "completed", second.error
                raw = json.loads(path.read_text(encoding="utf-8"))
                assert [part["content"] for message in raw["messages"] for part in message["parts"] if part["part_kind"] == "tool-return" and part["tool_name"] == "read_authorized_text"] == [original, original]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    preview = next(message for message in sent[1]["messages"] if message["role"] == "tool")
    assert "各4000个字符" in preview["content"]
    for request in sent[2:]:
        assert next(message for message in request["messages"] if message["role"] == "tool") == preview
    assert "read_context_evidence" in [tool["function"]["name"] for tool in sent[0]["tools"]]
    assert all(request["tools"] == sent[0]["tools"] for request in sent)
    assert "中间原文937" in sent[-1]["messages"][-1]["content"] or any("中间原文937" in message.get("content", "") for message in sent[-1]["messages"] if message["role"] == "tool")


@pytest.mark.parametrize("change,reason,expected", [
    ("permission", "authorization_changed", "只读上限（只能看、不能改）：True"),
    ("model", "provider_changed", "型号deepseek-revised"),
])
def test_same_task_rebuilds_stale_baseline_when_authorized_boundary_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, reason: str, expected: str) -> None:
    sent: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return response()

    async def run() -> None:
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
                task = Task(goal="按当前允许范围回答", scope=scope)
                path = tmp_path / "record.json"
                first = await Conversation(path).ask(task.goal, document=None, scope=scope, task=task,
                    model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)))
                assert first.status == "completed", first.error
                opened = Conversation.open(path)
                task = opened.turns[-1].task
                if change == "permission":
                    task.approval.access.read_only = True
                else:
                    task.provider_config = task.provider_config.model_copy(update={"model": "deepseek-revised", "context_window_override": 128000})
                second = await opened.ask("按新设置继续", document=None, scope=scope, task=task,
                    model=OpenAIChatModel(task.provider_config.model, provider=DeepSeekProvider(openai_client=client)))
                assert second.status == "completed", second.error
                assert second.task.id == first.task.id == task.id
                assert second.task.requests == second.task.total_requests == 2
                assert first.task.requests == task.requests == task.total_requests == 1
                assert Conversation.open(path).turns[-1].task.requests == 2
                observation = second.request_attempts[0].observation
                assert observation.layout_version > first.request_attempts[0].observation.layout_version
                assert reason in observation.wire.change_reasons
                saved = json.loads(path.read_text(encoding="utf-8"))["request_layouts"]["main"]
                assert reason in saved["changes"][-1]["reasons"]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    assert expected in json.dumps(sent[-1]["messages"], ensure_ascii=False)


def test_final_turn_records_the_actual_empty_tool_table_and_change_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return response(tool="memory", arguments={"action": "list"}) if len(sent) == 1 else response()

    async def run() -> None:
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                task = Task(goal="最后一轮回答", scope=None, max_turns=2)
                path = tmp_path / "record.json"
                turn = await Conversation(path).ask(task.goal, document=None, task=task,
                    model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)))
                assert turn.status == "completed", turn.error
                layout = json.loads(path.read_text(encoding="utf-8"))["request_layouts"]["main"]
                assert layout["tools"] == []
                first, last = [attempt.observation for attempt in turn.request_attempts]
                assert last.layout_version > first.layout_version
                assert last.wire.tool_count == 0
                assert "final_turn_tools_removed" in last.wire.change_reasons
                new_task = await Conversation.open(path).ask("开始新的任务", document=None,
                    model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)))
                assert new_task.status == "completed", new_task.error
                assert new_task.request_attempts[0].observation.wire.change_reasons == ["new_task"]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    assert sent[0]["tools"] and sent[1].get("tools", []) == [] and sent[2]["tools"]


def test_memory_review_uses_its_own_layout_without_changing_main_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return response(tool="queue_memory_review") if len(sent) == 1 else response()

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client))
                path = tmp_path / "record.json"
                task = Task(goal="完成后复盘，再继续", scope=None)
                first = await Conversation(path).ask(task.goal, document=None, task=task, model=model)
                assert first.status == "completed", first.error
                assert [attempt.actor for attempt in first.request_attempts] == ["main", "main", "memory_review"]
                raw = json.loads(path.read_text(encoding="utf-8"))
                main = raw["request_layouts"]["main"]
                review = raw["request_layouts"]["memory_review"]
                assert [tool["name"] for tool in review["tools"]] == ["memory"]
                assert "交付后有限复盘" in review["instructions"] and "交付后有限复盘" not in main["instructions"]
                assert first.request_attempts[-1].observation.layout_version == review["version"]
                reopened = Conversation.open(path)
                continued = await reopened.ask("继续原工作", document=None, task=reopened.turns[-1].task, model=model)
                assert continued.status == "completed", continued.error
                after = json.loads(path.read_text(encoding="utf-8"))["request_layouts"]
                assert after["main"] == main
                assert after["memory_review"] == review

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    assert sent[3]["messages"][:len(sent[1]["messages"])] == sent[1]["messages"]
    assert sent[0]["tools"] == sent[1]["tools"] == sent[3]["tools"]
    assert [tool["function"]["name"] for tool in sent[2]["tools"]] == ["memory"]


def test_retry_adopts_a_supplement_once_without_rebuilding_the_sent_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []
    task = Task(goal="失败后按补充继续", scope=None)
    supplement = "新补充937：继续核对，保留原要求。"

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            task.supplements.append(Supplement(text=supplement))
            return httpx2.Response(503, json={"error": {"message": "synthetic temporary failure"}})
        return response(tool="memory", arguments={"action": "list"}) if len(sent) == 2 else response()

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with deepseek_model("synthetic-only", http_client=http) as model:
                path = tmp_path / "record.json"
                turn = await Conversation(path).ask(task.goal, document=None, task=task, model=model)
                assert turn.status == "completed", turn.error
                assert task.requests == task.total_requests == 3 and task.turns_used == 2
                assert [attempt.status for attempt in turn.request_attempts] == ["failed", "completed", "completed"]
                assert task.supplements[0].applied_request == 2
                assert {attempt.observation.layout_version for attempt in turn.request_attempts} == {1}
                raw = json.loads(path.read_text(encoding="utf-8"))
                assert len(raw["request_layouts"]["main"]["changes"]) == 1
                assert [part["content"] for message in raw["messages"] for part in message["parts"] if part["part_kind"] == "user-prompt"].count(supplement) == 1

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    for previous, current in zip(sent, sent[1:]):
        assert current["messages"][:len(previous["messages"])] == previous["messages"]
        assert current["tools"] == previous["tools"]
    assert [message.get("content") for message in sent[-1]["messages"]].count(supplement) == 1


def test_changed_project_guidance_is_effective_and_explains_the_new_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guidance = tmp_path / "AGENTS.md"
    guidance.write_text("项目约定：第一版937。", encoding="utf-8")
    sent: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return response()

    async def run() -> None:
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client))
                scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
                task = Task(goal="遵循项目指导", scope=scope)
                path = tmp_path / "record.json"
                first = await Conversation(path).ask(task.goal, document=None, scope=scope, task=task, model=model)
                assert first.status == "completed", first.error
                guidance.write_text("项目约定：第二版812。", encoding="utf-8")
                opened = Conversation.open(path)
                second = await opened.ask("按新指导继续", document=None, scope=scope, task=opened.turns[-1].task, model=model)
                assert second.status == "completed", second.error
                assert second.request_attempts[0].observation.layout_version > first.request_attempts[0].observation.layout_version
                assert "instructions_changed" in second.request_attempts[0].observation.wire.change_reasons

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
    assert "第二版812" in json.dumps(sent[-1]["messages"], ensure_ascii=False)
