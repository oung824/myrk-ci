"""Provider output limits at the actual SDK request boundary, using Mock only."""

import asyncio
import json

import httpx
import httpx2
import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.tools import ToolDefinition

from myrk.provider import ProviderConfig, configured_model, deepseek_model


@pytest.fixture(autouse=True)
def refuse_real_http(monkeypatch):
    async def refused_async(_transport, _request):
        pytest.fail("Provider contract reached a real asynchronous HTTP transport")

    def refused_sync(_transport, _request):
        pytest.fail("Provider contract reached a real synchronous HTTP transport")

    for module in {httpx, httpx2}:
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", refused_async)
        monkeypatch.setattr(module.HTTPTransport, "handle_request", refused_sync)
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


@pytest.mark.parametrize("factory", ["configured", "legacy"])
@pytest.mark.parametrize("limit,streaming", [(2048, False), (1024, True), (0, False), (None, False)])
def test_deepseek_factories_send_the_supported_output_limit(factory, limit, streaming):
    requests = []
    schema = {"type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False}

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        if streaming:
            chunk = {"id": "offline-stream-limit", "object": "chat.completion.chunk", "created": 1,
                "model": "deepseek-flash", "choices": [{"index": 0,
                    "delta": {"role": "assistant", "content": "Offline response."}, "finish_reason": "stop"}]}
            return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"})
        return httpx2.Response(200, json={"id": "offline-limit", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "Offline response."}, "finish_reason": "stop"}]})

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False) as http:
            connection = (configured_model("synthetic-only", ProviderConfig(), http_client=http)
                if factory == "configured" else deepseek_model("synthetic-only", http_client=http))
            async with connection as model:
                messages = [ModelRequest(parts=[UserPromptPart("Inspect the local output limit.")])]
                settings = OpenAIChatModelSettings(openai_reasoning_effort="high")
                if limit is not None:
                    settings["max_tokens"] = limit
                parameters = ModelRequestParameters(function_tools=[ToolDefinition(name="read_text",
                    description="Read the chosen text.", parameters_json_schema=schema)])
                if streaming:
                    async with model.request_stream(messages, settings, parameters) as response:
                        async for _event in response:
                            pass
                else:
                    await model.request(messages, settings, parameters)

    asyncio.run(run())
    assert len(requests) == 1
    payload = requests[0]
    assert "max_completion_tokens" not in payload
    if limit is None:
        assert "max_tokens" not in payload
    else:
        assert payload["max_tokens"] == limit
    assert payload["reasoning_effort"] == "high"
    assert payload["tools"][0]["function"]["name"] == "read_text"
    assert payload["tools"][0]["function"]["parameters"] == schema


def test_openai_compatible_provider_keeps_its_output_limit_field():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        assert str(request.url) == "https://example.test/v1/chat/completions"
        return httpx2.Response(200, json={"id": "offline-openai-limit", "object": "chat.completion", "created": 1,
            "model": "gpt-4o", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "Offline response."}, "finish_reason": "stop"}]})

    async def run():
        config = ProviderConfig(name="Compatible fixture", protocol="openai-chat",
            base_url="https://example.test/v1", model="gpt-4o")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                await model.request([ModelRequest(parts=[UserPromptPart("Inspect the local output limit.")])],
                    {"max_tokens": 32}, ModelRequestParameters())

    asyncio.run(run())
    assert len(requests) == 1
    assert requests[0]["max_completion_tokens"] == 32
    assert "max_tokens" not in requests[0]


@pytest.mark.parametrize("factory", ["configured", "legacy"])
def test_deepseek_output_compatibility_keeps_the_remaining_sdk_profile(factory):
    def never_send(_request):
        pytest.fail("Inspecting a model profile should not dispatch a request")

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(never_send), trust_env=False) as http:
            async with DeepSeekProvider(api_key="synthetic-only", http_client=http) as provider:
                original = OpenAIChatModel("deepseek-flash", provider=provider).profile
                connection = (configured_model("synthetic-only", ProviderConfig(), http_client=http)
                    if factory == "configured" else deepseek_model("synthetic-only", http_client=http))
                async with connection as model:
                    assert model.profile == {**original, "openai_chat_supports_max_completion_tokens": False}

    asyncio.run(run())
