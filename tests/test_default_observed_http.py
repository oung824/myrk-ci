import asyncio
from hashlib import sha256
from urllib.parse import urlsplit

import httpx2
import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelRequest, TextPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from myrk.provider import ProviderConfig, configured_model
from myrk.request_observation import observe_request
from myrk.task_control import RequestAttempt


@pytest.mark.parametrize(("protocol", "endpoint", "model_name", "read_timeout", "bypass_proxy"), [
    ("openai-chat", "https://openai.invalid/v1", "gpt-4.1", 600, False),
    ("google", "https://generativelanguage.googleapis.com", "gemini-2.5-flash", 1200, True),
])
def test_default_model_preserves_environment_routing_timeout_and_close(
    monkeypatch, protocol, endpoint, model_name, read_timeout, bypass_proxy,
):
    proxy_url = "http://proxy.invalid:9876"
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setenv("NO_PROXY", urlsplit(endpoint).hostname if bypass_proxy else "")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)

    initialized = {}
    sent = []
    closed = []
    original_init = httpx2.AsyncHTTPTransport.__init__
    original_close = httpx2.AsyncHTTPTransport.aclose
    original_exit = httpx2.AsyncHTTPTransport.__aexit__

    def track_init(transport, *args, **kwargs):
        original_init(transport, *args, **kwargs)
        proxy = kwargs.get("proxy")
        initialized[transport] = str(proxy.url) if proxy is not None else None

    async def send(transport, request):
        sent.append((transport, request))
        if protocol == "google":
            return httpx2.Response(200, json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": "完成"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 2, "thoughtsTokenCount": 0},
            })
        return httpx2.Response(200, json={
            "id": "local-reply", "created": 1, "object": "chat.completion", "model": model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "完成"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        })

    async def close(transport):
        closed.append(transport)
        await original_close(transport)

    async def exit_transport(transport, *args):
        closed.append(transport)
        await original_exit(transport, *args)

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "__init__", track_init)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "aclose", close)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "__aexit__", exit_transport)

    config = ProviderConfig(name="本地用例", protocol=protocol, base_url=endpoint, model=model_name)
    attempt = RequestAttempt(number=1, model=model_name, provider=endpoint)

    async def run():
        async with configured_model("synthetic-key", config) as model:
            result = await observe_request(model, attempt).request(
                [ModelRequest(parts=[UserPromptPart("核对默认连接")])], None, ModelRequestParameters(),
            )
            assert any(isinstance(part, TextPart) and part.content == "完成" for part in result.parts)
            assert len(sent) == 1
            terminal, request = sent[0]
            assert terminal not in closed
            proxy = initialized[terminal]
            if bypass_proxy:
                assert proxy is None
            else:
                assert proxy.rstrip("/") == proxy_url
            assert request.extensions["timeout"]["connect"] == 5
            assert request.extensions["timeout"]["read"] == read_timeout
            assert attempt.observation is not None
            assert attempt.observation.uses_native_usage
            assert attempt.observation.wire is not None
            assert attempt.observation.wire.body_sha256 == sha256(request.content).hexdigest()
            assert (attempt.input_tokens, attempt.output_tokens) == (9, 2)
        assert closed.count(terminal) == 1

    asyncio.run(run())
