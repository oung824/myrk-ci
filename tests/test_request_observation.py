import asyncio
import gzip
import json
from pathlib import Path

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent, models
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider

from myrk.conversation import Conversation, Task, Turn
from myrk.provider import ProviderConfig
from myrk.request_observation import observe_request
from myrk.task_control import RequestAttempt
from tests.support.observed_http import observed_mock_transport


async def usage_exchange(path: Path, usage: dict | None, *, openai: bool = False) -> Turn:
    def respond(request: httpx2.Request) -> httpx2.Response:
        chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "controlled",
                 "choices": [{"index": 0, "delta": {"content": "完成"}, "finish_reason": "stop"}]}
        if usage is not None:
            chunk["usage"] = usage
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
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
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
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
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
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
    closes = []

    class PayloadStream(httpx2.AsyncByteStream):
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        async def __aiter__(self):
            yield self.payload[:17]
            yield self.payload[17:]

        async def aclose(self) -> None:
            closes.append("response closed")

    def respond(request: httpx2.Request) -> httpx2.Response:
        payload = {"id": "synthetic", "object": "chat.completion.chunk" if streaming else "chat.completion", "created": 1,
                   "model": "deepseek-flash", "choices": [{"index": 0, "delta" if streaming else "message": {"role": "assistant", "content": "压缩回应"}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "prompt_tokens_details": {"cached_tokens": 80}}}
        body = f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n" if streaming else json.dumps(payload)
        return httpx2.Response(200, stream=PayloadStream(gzip.compress(body.encode())), headers={"content-type": "text/event-stream" if streaming else "application/json", "content-encoding": "gzip"})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
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
                assert closes == ["response closed"]
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
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
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
