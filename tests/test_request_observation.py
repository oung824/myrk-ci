import asyncio
from collections import Counter
import gzip
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

from myrk.conversation import Conversation, Task, TaskScope, Turn
from myrk.actions import Action
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig
from myrk.request_observation import observe_request
from myrk.review import review_action
from myrk.task_control import RequestAttempt, TaskBudget


async def usage_exchange(path: Path, usage: dict | None, *, openai: bool = False) -> Turn:
    def respond(request: httpx2.Request) -> httpx2.Response:
        chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "controlled",
                 "choices": [{"index": 0, "delta": {"content": "完成"}, "finish_reason": "stop"}]}
        if usage is not None:
            chunk["usage"] = usage
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.openai.com/v1" if openai else "https://api.deepseek.com", http_client=http, max_retries=0) as client:
            model = OpenAIChatModel("gpt-4.1" if openai else "deepseek-flash", provider=OpenAIProvider(openai_client=client) if openai else DeepSeekProvider(openai_client=client))
            task = Task(goal="回答", scope=None, provider_config=ProviderConfig(name="OpenAI", base_url="https://api.openai.com/v1", model="gpt-4.1", protocol="openai-chat")) if openai else None
            turn = await Conversation(path).ask("回答", document=None, model=model, task=task)
            assert turn.status == "completed", turn.error
            return Conversation.open(path).turns[0]


def test_each_request_persists_cache_usage_from_the_final_sdk_exchange(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        chunk = {
            "id": "synthetic-response", "object": "chat.completion.chunk", "created": 1,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "已完成。"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010,
                      "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200,
                      "prompt_tokens_details": {"cached_tokens": 800}},
        }
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                conversation = Conversation(tmp_path / "record.json")
                turn = await conversation.ask("只回答。", document=None, model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)))
                assert turn.status == "completed", turn.error
                attempt = Conversation.open(conversation.path).turns[0].request_attempts[0]
                observation = attempt.observation
                assert observation is not None
                assert (observation.input_tokens, observation.cache_read_tokens, observation.cache_write_tokens) == (1000, 800, None)
                assert observation.cache_read_source == "usage.prompt_tokens_details.cached_tokens"
                assert observation.raw_usage["prompt_cache_hit_tokens"] == 800
                assert observation.field_presence["prompt_tokens_details.cached_tokens"] is True
                assert observation.protocol == "deepseek"
                assert observation.wire is not None and observation.wire.message_count == len(sent[0]["messages"])
                assert observation.elapsed_seconds is not None and observation.first_output_seconds is not None
                assert observation.first_output_seconds <= observation.elapsed_seconds
                assert "只回答" not in observation.model_dump_json()
                assert "synthetic-only" not in conversation.path.read_text(encoding="utf-8")

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("usage,expected,present", [
    (None, (None, None, None), False),
    ({"completion_tokens": 2, "total_tokens": 2}, (None, 2, None), True),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "prompt_cache_hit_tokens": 0,
      "prompt_cache_miss_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0}}, (0, 0, 0), True),
])
def test_absent_and_explicit_zero_usage_remain_distinct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage: dict | None, expected: tuple, present: bool) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    turn = asyncio.run(usage_exchange(tmp_path / "presence.json", usage))
    attempt = turn.request_attempts[0]
    observation = attempt.observation
    assert observation is not None
    assert (observation.input_tokens, observation.output_tokens, observation.cache_read_tokens) == expected
    assert (attempt.input_tokens, attempt.output_tokens) == expected[:2]
    assert observation.usage_present is present
    assert observation.field_presence["prompt_tokens"] is (expected[0] is not None)
    assert observation.cache_write_tokens is None


@pytest.mark.parametrize("usage,expected", [
    ({"completion_tokens": 5, "prompt_cache_hit_tokens": 1}, (None, 5)),
    ({"completion_tokens": 5}, (None, 5)),
    ({"prompt_tokens": 100}, (100, None)),
    ({"prompt_tokens": 0, "completion_tokens": 0}, (0, 0)),
    ({"prompt_tokens": 100, "completion_tokens": 5}, (100, 5)),
])
@pytest.mark.parametrize("openai", [False, True])
def test_turn_totals_preserve_each_reported_counter_without_promoting_sdk_defaults_to_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage: dict, expected: tuple, openai: bool) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    turn = asyncio.run(usage_exchange(tmp_path / "partial-total.json", usage, openai=openai))
    assert (turn.request_attempts[0].observation.input_tokens, turn.request_attempts[0].observation.output_tokens) == expected
    assert (turn.request_attempts[0].input_tokens, turn.request_attempts[0].output_tokens) == expected
    assert (turn.input_tokens, turn.output_tokens) == expected


def test_deepseek_top_level_cache_count_has_an_explicit_fallback_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        chunk = {
            "id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
            "choices": [{"index": 0, "delta": {"content": "完成"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010,
                      "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200},
        }
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                turn = await Conversation(tmp_path / "fallback.json").ask("回答", document=None, model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)))
                observation = turn.request_attempts[0].observation
                assert observation is not None
                assert observation.cache_read_tokens == 800
                assert observation.cache_read_source == "usage.prompt_cache_hit_tokens"
                assert observation.field_presence["prompt_tokens_details.cached_tokens"] is False
                assert observation.sdk_usage["cache_read_tokens"] == 0

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("cached,hit,miss", [(700, 800, 200), (800, 800, 300)])
def test_conflicting_cache_counts_keep_raw_evidence_and_unknown_read_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached: int, hit: int, miss: int) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    turn = asyncio.run(usage_exchange(tmp_path / "conflict.json", {
        "prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010,
        "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss,
        "prompt_tokens_details": {"cached_tokens": cached},
    }))
    observation = turn.request_attempts[0].observation
    assert observation is not None
    assert observation.cache_read_tokens is None and observation.cache_read_source is None
    assert observation.input_tokens == 1000
    assert observation.raw_usage["prompt_tokens_details.cached_tokens"] == cached
    assert observation.raw_usage["prompt_cache_hit_tokens"] == hit
    assert observation.usage_status == "conflict" and observation.conflicts


def test_openai_chat_reports_nested_cache_read_and_write_without_deepseek_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    turn = asyncio.run(usage_exchange(tmp_path / "openai.json", {
        "prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010,
        "prompt_cache_hit_tokens": 999,
        "prompt_tokens_details": {"cached_tokens": 700, "cache_write_tokens": 200},
    }, openai=True))
    observation = turn.request_attempts[0].observation
    assert observation is not None
    assert (observation.input_tokens, observation.cache_read_tokens, observation.cache_write_tokens) == (1000, 700, 200)
    assert observation.cache_write_source == "usage.prompt_tokens_details.cache_write_tokens"
    assert observation.protocol == "openai-chat" and observation.usage_status == "complete"


@pytest.mark.parametrize("streaming", [True, False])
def test_compressed_responses_keep_usage_and_decoded_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streaming: bool) -> None:
    class PayloadStream(httpx2.AsyncByteStream):
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        async def __aiter__(self):
            yield self.payload[:17]
            yield self.payload[17:]

    def respond(request: httpx2.Request) -> httpx2.Response:
        payload = {"id": "synthetic", "object": "chat.completion.chunk" if streaming else "chat.completion", "created": 1,
                   "model": "deepseek-flash", "choices": [{"index": 0, "delta" if streaming else "message": {"role": "assistant", "content": "压缩回应"}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "prompt_tokens_details": {"cached_tokens": 80}}}
        body = f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n" if streaming else json.dumps(payload)
        return httpx2.Response(200, stream=PayloadStream(gzip.compress(body.encode())), headers={"content-type": "text/event-stream" if streaming else "application/json", "content-encoding": "gzip"})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client))
                if streaming:
                    turn = await Conversation(tmp_path / "compressed.json").ask("回答", document=None, model=model)
                    assert turn.status == "completed" and turn.answer == "压缩回应"
                    attempt = turn.request_attempts[0]
                else:
                    attempt = RequestAttempt(number=1, model=model.model_name, provider="deepseek")
                    result = await Agent(observe_request(model, attempt)).run("回答")
                    assert result.output == "压缩回应"
                assert attempt.observation is not None
                assert (attempt.observation.input_tokens, attempt.observation.cache_read_tokens) == (100, 80)

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("reported", [False, True])
def test_interrupted_stream_keeps_partial_output_known_usage_and_unknown_remainder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reported: bool) -> None:
    delivered: list[str] = []
    sent = []

    class Interrupted(httpx2.AsyncByteStream):
        async def __aiter__(self):
            payload = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
                       "choices": [{"index": 0, "delta": {"content": "部分回答"}, "finish_reason": None}]}
            if reported:
                payload["usage"] = {"prompt_tokens": 100, "completion_tokens": 2, "total_tokens": 102,
                                    "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 10}}
            yield f"data: {json.dumps(payload)}\n\n".encode()
            assert "部分回答" in delivered, "观测不得预读下一块而阻塞首次显示"
            if reported:
                payload["choices"] = []
                payload["usage"] = {"completion_tokens": 4}
                yield f"data: {json.dumps(payload)}\n\n".encode()
            raise httpx2.ReadError("synthetic disconnection")

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, stream=Interrupted(), headers={"content-type": "text/event-stream"})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                conversation = Conversation(tmp_path / "interrupted.json")
                turn = await conversation.ask("回答", document=None, model=OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client)), on_update=lambda update: delivered.append(update.text) if update.kind == "text" else None)
                assert turn.status == "failed" and turn.answer == "部分回答"
                assert len(sent) == turn.requests == 1
                attempt = Conversation.open(conversation.path).turns[0].request_attempts[0]
                assert attempt.status == "failed"
                observation = attempt.observation
                assert observation is not None and observation.elapsed_seconds is not None
                assert observation.first_output_seconds is not None
                assert (observation.input_tokens, observation.cache_read_tokens) == ((100, 80) if reported else (None, None))
                assert observation.output_tokens == (4 if reported else None)
                assert observation.field_presence["prompt_tokens"] is reported
                assert observation.usage_status == ("partial" if reported else "unknown")

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_rebinding_an_injected_chat_model_preserves_attempt_ownership_and_endpoint_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.content)
        return httpx2.Response(200, json={"id": "synthetic", "object": "chat.completion", "created": 1, "model": "controlled",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "完成"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(bodies) * 100, "completion_tokens": 10, "total_tokens": len(bodies) * 100 + 10,
                      "prompt_tokens_details": {"cached_tokens": 0}}})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
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
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
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


@pytest.mark.parametrize("openai", [False, True])
def test_later_measured_reviews_preserve_earlier_unknown_totals_per_counter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openai: bool) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "proof.txt").write_text("授权材料", encoding="utf-8")
    targets = [tmp_path / f"approved-{number}.txt" for number in range(3)]
    review_calls = 0
    review_usage = [
        {"completion_tokens": 5, "prompt_cache_hit_tokens": 1},
        {"prompt_tokens": 100, "completion_tokens": 7},
        {"prompt_tokens": 200, "prompt_tokens_details": {"cached_tokens": 1}},
        {"prompt_tokens": 100, "completion_tokens": 7},
        {"prompt_tokens": 300, "completion_tokens": 9},
    ]

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal review_calls
        messages = json.loads(request.content)["messages"]
        reviewing = "你是独立动作审核者" in str([message for message in messages if message["role"] in {"system", "developer"}])
        tool = None
        if reviewing:
            usage = review_usage[review_calls]
            review_calls += 1
            if review_calls in {1, 3}:
                tool = ("read_text", {"path": "proof.txt"})
            answer = json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "已核对用户允许的确切小文件及其内容，动作符合授权。", "same_denied_outcome": False})
        else:
            usage = {"prompt_tokens": 100, "completion_tokens": 3}
            results = [message for message in messages if message["role"] == "tool"]
            if len(results) < 3:
                tool = ("create_text", {"path": str(targets[len(results)]), "content": "approved"})
            answer = "三个文件已创建，等待验收。"
        delta = {"role": "assistant"}
        if tool is not None:
            name, arguments = tool
            delta["tool_calls"] = [{"index": 0, "id": f"call-{reviewing}-{review_calls}-{len(messages)}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]
        else:
            delta["content"] = answer
        chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "controlled", "usage": usage,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if tool else "stop"}]}
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.openai.com/v1" if openai else "https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("gpt-4.1" if openai else "deepseek-flash", provider=OpenAIProvider(openai_client=client) if openai else DeepSeekProvider(openai_client=client))
                scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
                task = Task(goal="创建三个明确指定的文件", scope=scope, approval=ApprovalSettings(mode="auto_review"))
                if openai:
                    task.provider_config = ProviderConfig(name="OpenAI", base_url="https://api.openai.com/v1", model="gpt-4.1", protocol="openai-chat")
                path = tmp_path / "records" / "cumulative.json"
                turn = await Conversation(path).ask(task.goal, document=None, scope=scope, task=task, model=model, review_model=model)
                assert turn.status == "completed", turn.error
                assert [target.read_text(encoding="utf-8") for target in targets] == ["approved"] * 3
                saved = Conversation.open(path).turns[0]
                reviews = [action.review for action in saved.actions if action.review is not None]
                assert [(review.input_tokens, review.output_tokens) for review in reviews] == [(None, 12), (300, None), (300, 9)]
                assert [review.unmeasured_requests for review in reviews] == [1, 1, 0]
                assert [(item.input_tokens, item.output_tokens) for review in reviews for item in review.request_attempts] == [(None, 5), (100, 7), (200, None), (100, 7), (300, 9)]
                assert (saved.task.review_input_tokens, saved.task.review_output_tokens) == (None, None)
                assert saved.task.review_unmeasured_requests == 2
                assert saved.task.review_requests == review_calls == 5
                assert saved.task.requests == saved.task.turns_used == saved.task.total_requests == 9
                assert (saved.input_tokens, saved.output_tokens) == (400, 12)
                assert [item.number for review in reviews for item in review.request_attempts] == [2, 3, 5, 6, 8]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("usages,prior,expected_records,expected_total,expected_unmeasured,attempts", [
    ([{"prompt_tokens": 100, "completion_tokens": 5}] * 2, None, [(100, 5), (100, 5)], (200, 10), 0, 2),
    ([{"completion_tokens": 5, "prompt_cache_hit_tokens": 1}, {"prompt_tokens": 100, "completion_tokens": 5}], None, [(None, 5), (100, 5)], (None, 10), 1, 2),
    ([{"prompt_tokens": 100, "completion_tokens": 5}, {"completion_tokens": 5, "prompt_cache_hit_tokens": 1}], None, [(100, 5), (None, 5)], (None, 10), 1, 2),
    ([{"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 1}}, {"prompt_tokens": 100, "completion_tokens": 5}], None, [(100, None), (100, 5)], (200, None), 1, 2),
    ([{"prompt_tokens": 100, "completion_tokens": 5}, {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 1}}], None, [(100, 5), (100, None)], (200, None), 1, 2),
    ([None, {"prompt_tokens": 100, "completion_tokens": 5}], None, [(None, None), (100, 5)], (None, None), 1, 2),
    ([{"prompt_tokens": 0, "completion_tokens": 0}, {"prompt_tokens": 100, "completion_tokens": 5}], None, [(0, 0), (100, 5)], (100, 5), 0, 2),
    ([{"prompt_tokens": 100, "completion_tokens": 5}] * 2, (None, 7), [(100, 5), (100, 5)], (None, 17), 1, 2),
    ([{"prompt_tokens": 100, "completion_tokens": 5}] * 2, (11, None), [(100, 5), (100, 5)], (211, None), 1, 2),
    ([{"prompt_tokens": 100, "completion_tokens": 5}] * 2, (None, None), [(100, 5), (100, 5)], (None, None), 1, 2),
    ([503, {"prompt_tokens": 100, "completion_tokens": 5}], None, [(None, None), (100, 5)], (None, None), 1, 3),
])
@pytest.mark.parametrize("openai", [False, True])
def test_concurrent_complete_reviews_keep_known_account_totals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usages: list[dict | int | None], prior: tuple | None, expected_records: list[tuple], expected_total: tuple, expected_unmeasured: int, attempts: int, openai: bool) -> None:
    async def run() -> None:
        ready = asyncio.Event()
        sent = 0
        task = Task(goal="审核两个互不依赖的确切文件动作", scope=None, request_limit=10, max_turns=10, approval=ApprovalSettings(mode="auto_review"))
        if prior is not None:
            task.requests = task.total_requests = task.turns_used = task.review_requests = 1
            task.review_input_tokens, task.review_output_tokens = prior
            task.review_unmeasured_requests = 1
            prior_path = tmp_path / "prior-account.json"
            prior_path.write_text(task.model_dump_json(), encoding="utf-8")
            task = Task.model_validate_json(prior_path.read_text(encoding="utf-8"))
        budget = TaskBudget(task)
        actions = [Action(task_id=task.id, kind="create", target=str(tmp_path / f"approved-{number}.txt"), impact="创建已明确授权的小文件", approval_settings=task.approval) for number in range(2)]
        path = tmp_path / "reviews.json"

        def save() -> None:
            path.write_text(json.dumps({"task": task.model_dump(mode="json"), "actions": [action.model_dump(mode="json") for action in actions]}), encoding="utf-8")

        async def respond(request: httpx2.Request) -> httpx2.Response:
            nonlocal sent
            usage = usages[sent] if sent < len(usages) else {"prompt_tokens": 100, "completion_tokens": 5}
            sent += 1
            if sent >= 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            assert task.review_requests >= 2, "两个请求均已 prepare 后才能返回"
            if isinstance(usage, int):
                return httpx2.Response(usage, json={"error": {"message": "controlled retry", "type": "server_error"}})
            answer = json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "用户明确允许确切的小文件内容，目标和影响均已核对。", "same_denied_outcome": False})
            chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                     }
            if usage is not None:
                chunk["usage"] = usage
            return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.openai.com/v1" if openai else "https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("gpt-4.1" if openai else "deepseek-flash", provider=OpenAIProvider(openai_client=client) if openai else DeepSeekProvider(openai_client=client))
                allowed = await asyncio.gather(*(review_action(action, model=model, budget=budget, directory=tmp_path,
                    messages=[], denials=[], save=save, context=lambda: "unchanged") for action in actions))
        assert allowed == [True, True]
        record = json.loads(path.read_text(encoding="utf-8"))
        saved_task = Task.model_validate(record["task"])
        saved_actions = [Action.model_validate(action) for action in record["actions"]]
        assert Counter((action.review.input_tokens, action.review.output_tokens) for action in saved_actions) == Counter(expected_records)
        assert (saved_task.review_input_tokens, saved_task.review_output_tokens) == expected_total
        assert saved_task.review_unmeasured_requests == expected_unmeasured
        previous = 1 if prior is not None else 0
        assert saved_task.review_requests == saved_task.requests == saved_task.total_requests == previous + attempts == previous + sent
        assert saved_task.turns_used == previous + 2
        assert saved_task.request_limit == saved_task.max_turns == 10
        new_attempts = [item for action in saved_actions for item in action.review.request_attempts]
        assert sorted(item.number for item in new_attempts) == list(range(previous + 1, previous + attempts + 1))
        assert sum(item.status == "failed" for item in new_attempts) == (1 if attempts == 3 else 0)

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("first_usage,finished_total,snapshot_unmeasured", [
    ({"prompt_tokens": 100, "completion_tokens": 5}, (200, 10), 1),
    ({"completion_tokens": 5, "prompt_cache_hit_tokens": 1}, (None, 10), 2),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 1}}, (200, None), 2),
])
@pytest.mark.parametrize("openai", [False, True])
def test_saved_inflight_review_totals_stay_unknown_after_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_usage: dict, finished_total: tuple, snapshot_unmeasured: int, openai: bool) -> None:
    async def run() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        (tmp_path / "proof.txt").write_text("已授权的审核资料", encoding="utf-8")
        scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
        task = Task(goal="保存正在等待的审核并恢复", scope=scope, approval=ApprovalSettings(mode="auto_review"))
        budget = TaskBudget(task)
        actions = [Action(task_id=task.id, kind="create", target=str(tmp_path / "approved.txt"), approval_settings=task.approval)]
        path = tmp_path / "inflight.json"
        calls = 0

        def save() -> None:
            path.write_text(json.dumps({"task": task.model_dump(mode="json"), "actions": [action.model_dump(mode="json") for action in actions]}), encoding="utf-8")

        async def respond(request: httpx2.Request) -> httpx2.Response:
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
            answer = json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "已核对确切文件及用户同意，动作符合明确授权。", "same_denied_outcome": False})
            delta = {"role": "assistant", "content": answer} if calls > 1 else {"role": "assistant", "tool_calls": [{"index": 0, "id": "read-proof", "type": "function", "function": {"name": "read_text", "arguments": '{"path":"proof.txt"}'}}]}
            chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": "stop" if calls > 1 else "tool_calls"}],
                     "usage": first_usage if calls == 1 else {"prompt_tokens": 100, "completion_tokens": 5}}
            return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.openai.com/v1" if openai else "https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("gpt-4.1" if openai else "deepseek-flash", provider=OpenAIProvider(openai_client=client) if openai else DeepSeekProvider(openai_client=client))
                pending = asyncio.create_task(review_action(actions[0], model=model, budget=budget, directory=tmp_path,
                    messages=[], denials=[], save=save, context=lambda: "unchanged"))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    snapshot = path.read_text(encoding="utf-8")
                    (tmp_path / "suspended-snapshot.json").write_text(snapshot, encoding="utf-8")
                    saved = json.loads(snapshot)
                    assert (saved["task"]["review_input_tokens"], saved["task"]["review_output_tokens"]) == (None, None)
                    assert (saved["actions"][0]["review"]["input_tokens"], saved["actions"][0]["review"]["output_tokens"]) == (None, None)
                    assert (saved["task"]["review_requests"], saved["task"]["review_unmeasured_requests"]) == (2, snapshot_unmeasured)
                finally:
                    release.set()
                    assert await pending
                assert (task.review_input_tokens, task.review_output_tokens) == finished_total
                restored = Task.model_validate_json(json.dumps(saved["task"]))
                restored_budget = TaskBudget(restored)
                next_action = Action(task_id=restored.id, kind="create", target=str(tmp_path / "restored.txt"), approval_settings=restored.approval)
                restored_path = tmp_path / "restored.json"
                assert await review_action(next_action, model=model, budget=restored_budget, directory=tmp_path,
                    messages=[], denials=[], save=lambda: restored_path.write_text(restored.model_dump_json(), encoding="utf-8"), context=lambda: "unchanged")
                saved_restored = Task.model_validate_json(restored_path.read_text(encoding="utf-8"))
                assert (saved_restored.review_input_tokens, saved_restored.review_output_tokens) == (None, None)
                assert saved_restored.review_unmeasured_requests == snapshot_unmeasured
                assert saved_restored.review_requests == saved_restored.requests == saved_restored.turns_used == saved_restored.total_requests == 3

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
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
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
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com", http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client))
                attempt = RequestAttempt(number=1, model=model.model_name, provider="deepseek")
                await Agent(observe_request(model, attempt, layout_version=2, change_reasons=["权限已变化"])).run("回答")
                assert attempt.observation.layout_version == 2
                assert attempt.observation.wire.change_reasons == ["权限已变化"]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("openai", [False, True])
def test_cache_read_cannot_exceed_total_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openai: bool) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    turn = asyncio.run(usage_exchange(tmp_path / "inconsistent-total.json", {
        "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
        "prompt_tokens_details": {"cached_tokens": 101},
    }, openai=openai))
    observation = turn.request_attempts[0].observation
    assert observation is not None
    assert observation.input_tokens == 100 and observation.cache_read_tokens is None
    assert observation.raw_usage["prompt_tokens_details.cached_tokens"] == 101
    assert observation.cache_read_source is None and observation.usage_status == "conflict"
