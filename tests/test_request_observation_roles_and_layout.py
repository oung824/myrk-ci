import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent, models
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider

from myrk.conversation import Conversation, Task, TaskScope
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig
from myrk.request_observation import observe_request
from myrk.task_control import RequestAttempt
from tests.support.observed_http import observed_mock_transport


def test_rebinding_an_injected_chat_model_preserves_attempt_ownership_and_endpoint_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.content)
        return httpx2.Response(200, json={"id": "synthetic", "object": "chat.completion", "created": 1, "model": "controlled",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "完成"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(bodies) * 100, "completion_tokens": 10, "total_tokens": len(bodies) * 100 + 10,
                      "prompt_tokens_details": {"cached_tokens": 0}}})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://example.test:8443/v1", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("gpt-4.1", provider=OpenAIProvider(openai_client=client))
                first = RequestAttempt(number=1, model=model.model_name, provider="synthetic")
                second = RequestAttempt(number=2, model=model.model_name, provider="synthetic")
                observed = observe_request(model, first)
                await Agent(observed).run("同一正文")
                await Agent(observe_request(observed, second)).run("同一正文")
                await Agent(model).run("同一正文")
                assert bodies[0] == bodies[1] == bodies[2], "观测不得改变SDK最终编码的请求正文"
                assert first.observation is not None and second.observation is not None
                assert (first.observation.input_tokens, second.observation.input_tokens) == (100, 200)
                assert first.observation.endpoint == "https://example.test:8443/v1/"
                assert first.observation.wire is not None and second.observation.wire is not None
                assert first.observation.wire.body_sha256 == second.observation.wire.body_sha256

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("usage,expected,child_total,child_review_total,turn_total", [
    ({"completion_tokens": 5, "prompt_cache_hit_tokens": 1}, (None, 5), (None, 10), (None, 15), (None, 30)),
    ({"completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 1}}, (None, 5), (None, 10), (None, 15), (None, 30)),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 1}}, (100, None), (200, None), (300, None), (600, None)),
    (None, (None, None), (None, None), (None, None), (None, None)),
    ({"prompt_tokens": 0, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0}}, (0, 0), (0, 0), (0, 0), (0, 0)),
    ({"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 20}}, (100, 5), (200, 10), (300, 15), (600, 30)),
    ({"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 101}}, (100, 5), (200, 10), (300, 15), (600, 30)),
])
@pytest.mark.parametrize("openai", [False, True])
@pytest.mark.parametrize("child_review", [False, True])
def test_main_child_memory_and_action_review_share_observation_without_duplicate_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage: dict | None, expected: tuple, child_total: tuple, child_review_total: tuple, turn_total: tuple, openai: bool, child_review: bool) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "notes.txt").write_text("实际原文", encoding="utf-8")
    target = tmp_path / "approved.txt"
    calls: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        messages = payload["messages"]
        instructions = str([message for message in messages if message["role"] in {"system", "developer"}])
        child = any(message["role"] == "user" and str(message["content"]).startswith("受委派工作：") for message in messages)
        actor = "action_review" if "你是独立动作审核者" in instructions else "memory_review" if "交付后有限复盘" in instructions else "delegation" if child else "main"
        calls.append(actor)
        tool_results = [message for message in messages if message["role"] == "tool"]
        tool = None
        if actor == "main" and not tool_results:
            tool = ("delegate_work", {"goal": "读取资料", "context": "只读notes.txt", "evidence_requirements": "原文", "expected_files": ["notes.txt"]})
        elif actor == ("delegation" if child_review else "main") and len(tool_results) == 1:
            tool = ("create_text", {"path": str(target), "content": "已核对"})
        elif actor == "delegation" and not tool_results:
            tool = ("read_text", {"path": "notes.txt"})
        delta = {"role": "assistant", "reasoning_content": "核对本次范围。"}
        if tool:
            name, args = tool
            delta["tool_calls"] = [{"index": 0, "id": f"call-{len(calls)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
        else:
            delta["content"] = json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "用户明确同意创建确切的小文件，目标及内容已经核实。", "same_denied_outcome": False}) if actor == "action_review" else "已核对。"
        chunk = {"id": f"synthetic-{len(calls)}", "object": "chat.completion.chunk", "created": 1, "model": "controlled",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if tool else "stop"}]}
        if usage is not None:
            chunk["usage"] = usage
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.openai.com/v1" if openai else "https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("gpt-4.1" if openai else "deepseek-flash", provider=OpenAIProvider(openai_client=client) if openai else DeepSeekProvider(openai_client=client))
                scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
                task = Task(goal="读取原文后创建指定文件并复盘", scope=scope, approval=ApprovalSettings(mode="auto_review"), memory_review_requested=True)
                if openai:
                    task.provider_config = ProviderConfig(name="OpenAI", base_url="https://api.openai.com/v1", model="gpt-4.1", protocol="openai-chat")
                conversation = Conversation(tmp_path / "records" / "main.json")
                turn = await conversation.ask(task.goal, document=None, task=task, scope=scope, model=model, review_model=model)
                assert turn.status == "completed", turn.error
                assert target.read_text(encoding="utf-8") == "已核对"
                turn = Conversation.open(conversation.path).turns[0]
                task = turn.task
                assert task is not None
                child = Conversation.open(Path(turn.delegations[0].record_path)).turns[0]
                review = next(action.review for action in turn.actions if action.review is not None)
                attempts = sorted([*turn.request_attempts, *child.request_attempts, *review.request_attempts], key=lambda item: item.number)
                assert calls == (["main", "delegation", "delegation", "action_review", "delegation", "main", "memory_review"] if child_review else ["main", "delegation", "delegation", "main", "action_review", "main", "memory_review"])
                assert [item.actor for item in attempts] == calls
                assert [item.number for item in attempts] == [1, 2, 3, 4, 5, 6, 7]
                assert task.total_requests == len(attempts) == 7
                assert [(item.observation.input_tokens, item.observation.output_tokens) for item in attempts] == [expected] * 7
                assert [(item.input_tokens, item.output_tokens) for item in attempts] == [expected] * 7
                assert all(item.observation.wire is not None and item.observation.first_output_seconds is not None for item in attempts)
                assert (child.input_tokens, child.output_tokens) == (child_review_total if child_review else child_total)
                assert (review.input_tokens, review.output_tokens) == expected
                account = turn.delegations[0] if child_review else task
                assert (account.review_input_tokens, account.review_output_tokens) == expected
                assert review.unmeasured_requests == account.review_unmeasured_requests == (1 if None in expected else 0)
                assert account.review_requests == 1
                assert (task.requests, task.turns_used) == ((3, 3) if child_review else (5, 5))
                assert (turn.delegations[0].requests, turn.delegations[0].turns_used) == ((4, 4) if child_review else (2, 2))
                assert child.request_account is not None
                if child_review:
                    assert (child.request_account.review_input_tokens, child.request_account.review_output_tokens) == expected
                    assert child.request_account.review_requests == 1
                    assert child.request_account.review_unmeasured_requests == account.review_unmeasured_requests
                assert task.memory_reviews == 1 and turn.memory_review_messages
                if usage is not None and "prompt_cache_hit_tokens" in usage and None in expected:
                    sdk_response = next(message for message in review.messages if isinstance(message, ModelResponse))
                    assert sdk_response.usage.has_values()
                    assert (sdk_response.usage.input_tokens, sdk_response.usage.output_tokens) == (0, 0)
                assert (turn.input_tokens, turn.output_tokens) == turn_total
                assert review.request_owner_id == (turn.delegations[0].id if child_review else None)
                assert all(item.delegation_id == turn.delegations[0].id for item in child.request_attempts)
                assert all(item.delegation_id == review.request_owner_id for item in review.request_attempts)

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_final_tool_filter_and_connection_retries_keep_one_summary_per_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        sent.append(payload)
        assert not payload.get("tools"), "最后办事轮不能因观测保留动作工具"
        if len(sent) == 1:
            return httpx2.Response(503, json={"error": {"message": "controlled temporary failure"}})
        chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
                 "choices": [{"index": 0, "delta": {"content": "交回已有结果"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 2, "total_tokens": 102, "prompt_tokens_details": {"cached_tokens": 0}}}
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                task = Task(goal="最后一轮", scope=None, max_turns=1)
                path = tmp_path / "retry.json"
                turn = await Conversation(path).ask(task.goal, document=None, task=task, model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)))
                assert turn.status == "completed" and task.turns_used == 1
                assert task.requests == task.total_requests == len(sent) == 2
                failed, completed = turn.request_attempts
                assert failed.status == "failed" and failed.observation.usage_status == "unknown"
                assert failed.observation.first_output_seconds is None and failed.observation.elapsed_seconds is not None
                assert completed.observation.cache_read_tokens == 0
                assert all(item.observation.wire.tool_count == 0 for item in turn.request_attempts)
                record = json.loads(path.read_text(encoding="utf-8"))
                for item in record["turns"][0]["request_attempts"]:
                    del item["observation"]
                path.write_text(json.dumps(record), encoding="utf-8")
                old = Conversation.open(path).turns[0]
                assert all(item.observation is None for item in old.request_attempts)
                assert old.task.requests == 2 and old.task.turns_used == old.task.max_turns == 1

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_layout_version_and_change_reason_are_attached_to_the_actual_wire_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"id": "synthetic", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "完成"}, "finish_reason": "stop"}]})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client))
                attempt = RequestAttempt(number=1, model=model.model_name, provider="deepseek")
                await Agent(observe_request(model, attempt, layout_version=2, change_reasons=["权限已变化"])).run("回答")
                assert attempt.observation.layout_version == 2
                assert attempt.observation.wire.change_reasons == ["权限已变化"]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
