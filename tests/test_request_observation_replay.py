"""Native chat response observations through the real SDK and local HTTP replies."""

import asyncio
from contextlib import nullcontext
import gzip
import json

import httpx2
import pytest
from pydantic_ai import models
from pydantic_ai.exceptions import UsageExtractionFailedWarning
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from myrk.conversation import Conversation
from myrk.provider import ProviderConfig, configured_model
from myrk.request_observation import observe_request
from myrk.task_control import RequestAttempt


@pytest.fixture(autouse=True)
def reject_default_transports(monkeypatch):
    attempted = []

    async def refused_async(_transport, _request):
        attempted.append("async")
        pytest.fail("An observation regression attempted real asynchronous HTTP")

    def refused_sync(_transport, _request):
        attempted.append("sync")
        pytest.fail("An observation regression attempted real synchronous HTTP")

    import httpx

    for module in (httpx, httpx2):
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", refused_async)
        monkeypatch.setattr(module.HTTPTransport, "handle_request", refused_sync)
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    yield
    assert attempted == []


def test_json_response_preserves_native_finish_and_reasoning():
    document = {"id": "synthetic-observation", "object": "chat.completion", "created": 1,
        "model": "deepseek-flash", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "Completed."}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 7}}}
    body = gzip.compress(json.dumps(document).encode())

    class Chunks(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield body[:13]
            yield body[13:]

    sends = []

    def respond(request):
        sends.append(request)
        return httpx2.Response(200, stream=Chunks(), headers={
            "content-type": "application/json", "content-encoding": "gzip"})

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False) as client:
            async with configured_model("synthetic-observation-only", ProviderConfig(), http_client=client) as model:
                attempt = RequestAttempt(number=1, model=model.model_name, provider="deepseek", purpose="compaction")
                observed = observe_request(model, attempt)
                await observed.request([ModelRequest(parts=[UserPromptPart("Answer locally.")])],
                    None, ModelRequestParameters())
                return attempt.observation

    observation = asyncio.run(run())
    assert len(sends) == 1 and observation is not None
    assert (observation.finish_reason, observation.thinking_tokens, observation.output_tokens) == ("stop", 7, 10)
    assert observation.thinking_source == "usage.completion_tokens_details.reasoning_tokens"
    assert observation.field_presence["completion_tokens_details.reasoning_tokens"] is True
    assert observation.raw_usage["completion_tokens_details.reasoning_tokens"] == 7


@pytest.fixture
def exchange_reply():
    def exchange(response, *, streaming=False, config=None):
        sends = []

        def respond(request):
            sends.append(request)
            return response

        async def run():
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False) as client:
                async with configured_model("synthetic-observation-only", config or ProviderConfig(), http_client=client) as model:
                    attempt = RequestAttempt(number=1, model=model.model_name, provider=model.system)
                    observed = observe_request(model, attempt)
                    messages = [ModelRequest(parts=[UserPromptPart("Answer locally.")])]
                    if streaming:
                        async with observed.request_stream(messages, None, ModelRequestParameters()) as result:
                            async for _event in result:
                                pass
                            sdk_response = result.get()
                    else:
                        sdk_response = await observed.request(messages, None, ModelRequestParameters())
                    return attempt.observation, sdk_response

        observation, sdk_response = asyncio.run(run())
        assert len(sends) == 1 and observation is not None
        return observation, sdk_response

    return exchange


@pytest.mark.parametrize("compressed", [False, True])
def test_sse_finish_arrives_after_first_output_and_survives_usage_only_tail(exchange_reply, compressed):
    events = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "offline-read", "type": "function", "function": {"name": "read_text", "arguments": "{}"}}]},
            "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22,
            "completion_tokens_details": {"reasoning_tokens": 0}}},
    ]
    text = "".join("data: " + json.dumps({"id": "offline-tool", "object": "chat.completion.chunk",
        "created": 1, "model": "deepseek-flash", **event}) + "\n\n" for event in events) + "data: [DONE]\n\n"
    body = gzip.compress(text.encode()) if compressed else text.encode()

    class Chunks(httpx2.AsyncByteStream):
        async def __aiter__(self):
            for start in range(0, len(body), 11):
                yield body[start:start + 11]

    headers = {"content-type": "text/event-stream"}
    if compressed:
        headers["content-encoding"] = "gzip"
    observation, sdk_response = exchange_reply(httpx2.Response(200, stream=Chunks(), headers=headers), streaming=True)
    assert (observation.finish_reason, observation.thinking_tokens, observation.output_tokens) == ("tool_calls", 0, 2)
    assert sdk_response.finish_reason == "tool_call"
    assert observation.first_output_seconds is not None
    assert observation.field_presence["completion_tokens_details.reasoning_tokens"] is True
    assert observation.thinking_source == "usage.completion_tokens_details.reasoning_tokens"


@pytest.mark.parametrize("details,expected,present", [
    (None, None, False),
    ({}, None, False),
    ({"reasoning_tokens": None}, None, True),
    ({"reasoning_tokens": False}, None, True),
    ({"reasoning_tokens": -1}, None, True),
    ({"reasoning_tokens": "7"}, None, True),
    ({"reasoning_tokens": 0}, 0, True),
])
def test_reported_reasoning_is_distinct_from_missing_zero_and_invalid_values(exchange_reply, details, expected, present):
    usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
    if details is not None:
        usage["completion_tokens_details"] = details
    response = {"id": "offline-presence", "object": "chat.completion", "created": 1,
        "model": "deepseek-flash", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "Completed."}, "finish_reason": "stop"}], "usage": usage}
    warning = isinstance(details, dict) and details.get("reasoning_tokens") == -1
    with pytest.warns(UsageExtractionFailedWarning) if warning else nullcontext():
        observation, _response = exchange_reply(httpx2.Response(200, json=response))
    assert observation.thinking_tokens == expected
    assert observation.field_presence["completion_tokens_details.reasoning_tokens"] is present
    assert observation.thinking_source == ("usage.completion_tokens_details.reasoning_tokens" if expected is not None else None)
    assert observation.raw_usage.get("completion_tokens_details.reasoning_tokens") == expected
    assert observation.output_tokens == 10


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat"])
def test_missing_native_finish_is_not_filled_from_sdk_defaults(exchange_reply, protocol):
    response = {"id": "offline-missing-finish", "object": "chat.completion", "created": 1,
        "model": "deepseek-flash", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "Completed."}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}
    config = ProviderConfig() if protocol == "deepseek" else ProviderConfig(
        name="OpenAI", protocol="openai-chat", base_url="https://api.openai.com/v1", model="gpt-4.1")
    observation, _response = exchange_reply(httpx2.Response(200, json=response), config=config)
    assert observation.finish_reason is None
    assert observation.thinking_tokens is None
    assert observation.thinking_source is None
    assert observation.output_tokens == 10


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter"])
def test_native_finish_is_retained_without_any_usage_payload(exchange_reply, reason):
    response = {"id": "offline-no-usage", "object": "chat.completion", "created": 1,
        "model": "deepseek-flash", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "Received text."}, "finish_reason": reason}]}
    observation, _response = exchange_reply(httpx2.Response(200, json=response))
    assert observation.finish_reason == reason
    assert observation.usage_present is False and observation.usage_status == "unknown"
    assert observation.input_tokens is observation.output_tokens is observation.thinking_tokens is None


def test_interrupted_sse_persists_reported_reasoning_without_inventing_a_finish(tmp_path):
    delivered, sends = [], []

    class Interrupted(httpx2.AsyncByteStream):
        async def __aiter__(self):
            first = {"id": "offline-interrupted", "object": "chat.completion.chunk", "created": 1,
                "model": "deepseek-flash", "choices": [{"index": 0,
                    "delta": {"role": "assistant", "content": "Partial answer."}, "finish_reason": None}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
                    "completion_tokens_details": {"reasoning_tokens": 7}}}
            yield ("data: " + json.dumps(first) + "\n\n").encode()
            assert "Partial answer." in delivered, "Observing usage must not pre-read the next chunk"
            later = {"id": "offline-interrupted", "object": "chat.completion.chunk", "created": 1,
                "model": "deepseek-flash", "choices": [], "usage": {"completion_tokens": 12}}
            yield ("data: " + json.dumps(later) + "\n\n").encode()
            raise httpx2.ReadError("synthetic interruption after reported usage")

    def respond(request):
        sends.append(request)
        return httpx2.Response(200, stream=Interrupted(), headers={"content-type": "text/event-stream"})

    async def run():
        path = tmp_path / "interrupted-observation.json"
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False) as client:
            async with configured_model("synthetic-observation-only", ProviderConfig(), http_client=client) as model:
                turn = await Conversation(path).ask("Answer locally.", document=None, model=model,
                    on_update=lambda update: delivered.append(update.text) if update.kind == "text" else None)
        assert turn.status == "failed" and turn.answer == "Partial answer."
        return Conversation.open(path).turns[0]

    with pytest.warns(UsageExtractionFailedWarning, match="usage.prompt_tokens"):
        saved = asyncio.run(run())
    assert saved.requests == len(sends) == 1
    assert saved.output_tokens is None
    attempt = saved.request_attempts[0]
    assert attempt.status == "failed" and attempt.observation is not None
    observation = attempt.observation
    assert (observation.input_tokens, observation.output_tokens, observation.thinking_tokens) == (20, 12, 7)
    assert observation.finish_reason is None
    assert observation.thinking_source == "usage.completion_tokens_details.reasoning_tokens"
    assert observation.field_presence["completion_tokens_details.reasoning_tokens"] is True
    assert observation.usage_status == "partial"
