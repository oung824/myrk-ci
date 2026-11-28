import asyncio
import gzip
import json

import httpx2
import pytest
from pydantic_ai import Agent, models

from myrk.provider import ProviderConfig, configured_model
from myrk.request_observation import observe_request
from myrk.task_control import RequestAttempt
from tests.support.observed_http import observed_mock_transport


def test_gemini_stream_keeps_the_last_usage_event_without_a_trailing_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": "完成"}]}, "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "thoughtsTokenCount": 0, "cachedContentTokenCount": 0}}

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        async with httpx2.AsyncClient(transport=observed_mock_transport(lambda request: httpx2.Response(200, text=f"data: {json.dumps(chunk)}", headers={"content-type": "text/event-stream"}))) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                attempt = RequestAttempt(number=1, model=config.model, provider="google")
                async with Agent(observe_request(model, attempt)).run_stream("回答") as result:
                    await result.get_output()
        assert attempt.observation is not None
        assert (attempt.observation.input_tokens, attempt.observation.output_tokens, attempt.observation.thinking_tokens, attempt.observation.cache_read_tokens) == (3, 2, 0, 0)
        assert attempt.observation.finish_reason == "STOP"

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("usage,expected", [
    (None, (None, None, None, None)),
    ({"promptTokenCount": 100, "candidatesTokenCount": 5, "totalTokenCount": 112}, (100, None, None, None)),
    ({"promptTokenCount": 100, "candidatesTokenCount": 5, "thoughtsTokenCount": 0, "cachedContentTokenCount": 0}, (100, 5, 0, 0)),
    ({"candidatesTokenCount": 5, "thoughtsTokenCount": 7}, (None, 12, 7, None)),
    ({"promptTokenCount": 0, "candidatesTokenCount": 0, "thoughtsTokenCount": 0, "cachedContentTokenCount": 0}, (0, 0, 0, 0)),
])
@pytest.mark.parametrize("streaming", [False, True])
def test_gemini_unknown_counters_are_distinct_from_reported_zero(monkeypatch: pytest.MonkeyPatch, usage: dict | None, expected: tuple, streaming: bool) -> None:
    chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": "完成"}]}, "finishReason": "STOP"}]}
    if usage is not None:
        chunk["usageMetadata"] = usage

    def respond(request: httpx2.Request) -> httpx2.Response:
        if streaming:
            return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\n", headers={"content-type": "text/event-stream"})
        return httpx2.Response(200, json=chunk)

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                attempt = RequestAttempt(number=1, model=config.model, provider="google")
                agent = Agent(observe_request(model, attempt))
                if streaming:
                    async with agent.run_stream("回答") as result:
                        await result.get_output()
                else:
                    await agent.run("回答")
        observation = attempt.observation
        assert observation is not None
        assert (observation.input_tokens, observation.output_tokens, observation.thinking_tokens, observation.cache_read_tokens) == expected
        assert (attempt.input_tokens, attempt.output_tokens) == expected[:2]
        assert observation.usage_present is (usage is not None)
        assert observation.field_presence.get("thoughtsTokenCount", False) is (expected[2] is not None)
        assert observation.field_presence.get("cachedContentTokenCount", False) is (expected[3] is not None)
        assert observation.cache_write_tokens is None
        assert observation.raw_usage == (usage or {})

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_gemini_signature_only_chunk_is_not_the_first_output_and_cumulative_usage_survives_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    attempt = RequestAttempt(number=1, model="gemini-2.5-flash", provider="google")

    class Chunks(httpx2.AsyncByteStream):
        async def __aiter__(self):
            initial = {"candidates": [{"content": {"role": "model", "parts": [{"text": "", "thought": True, "thoughtSignature": "c2lnbmF0dXJl"}]}}],
                       "usageMetadata": {"promptTokenCount": 100, "cachedContentTokenCount": 80}}
            yield f"data: {json.dumps(initial)}\n\n".encode()
            assert attempt.observation.first_output_seconds is None
            thought = {"candidates": [{"content": {"role": "model", "parts": [{"text": "核对", "thought": True}]}}], "usageMetadata": {"thoughtsTokenCount": 7}}
            yield f"data: {json.dumps(thought)}\n\n".encode()
            assert attempt.observation.first_output_seconds is not None
            final = {"candidates": [{"content": {"role": "model", "parts": [{"text": "完成"}]}, "finishReason": "STOP"}], "usageMetadata": {"candidatesTokenCount": 5}}
            yield f"data: {json.dumps(final)}\n\n".encode()

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model=attempt.model, protocol="google")
        async with httpx2.AsyncClient(transport=observed_mock_transport(lambda request: httpx2.Response(200, stream=Chunks(), headers={"content-type": "text/event-stream"}))) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                async with Agent(observe_request(model, attempt)).run_stream("回答") as result:
                    assert await result.get_output() == "完成"
        observation = attempt.observation
        assert (observation.input_tokens, observation.output_tokens, observation.thinking_tokens, observation.cache_read_tokens) == (100, 12, 7, 80)
        assert observation.raw_usage == {"promptTokenCount": 100, "cachedContentTokenCount": 80, "thoughtsTokenCount": 7, "candidatesTokenCount": 5}

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("streaming", [False, True])
def test_gemini_compressed_exchange_keeps_decoded_usage_and_output(monkeypatch: pytest.MonkeyPatch, streaming: bool) -> None:
    chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": "完成"}]}, "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "thoughtsTokenCount": 0}}
    data = (f"data: {json.dumps(chunk)}\n\n" if streaming else json.dumps(chunk)).encode()
    closes = []

    class Payload(httpx2.AsyncByteStream):
        async def __aiter__(self):
            compressed = gzip.compress(data)
            yield compressed[:17]
            yield compressed[17:]

        async def aclose(self) -> None:
            closes.append("response closed")

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        async with httpx2.AsyncClient(transport=observed_mock_transport(lambda request: httpx2.Response(200, stream=Payload(), headers={"content-type": "text/event-stream" if streaming else "application/json", "content-encoding": "gzip"}))) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                attempt = RequestAttempt(number=1, model=config.model, provider="google")
                agent = Agent(observe_request(model, attempt))
                if streaming:
                    async with agent.run_stream("回答") as result:
                        assert await result.get_output() == "完成"
                else:
                    assert (await agent.run("回答")).output == "完成"
        assert closes == ["response closed"]
        assert attempt.observation is not None
        assert (attempt.observation.input_tokens, attempt.observation.output_tokens, attempt.observation.thinking_tokens) == (3, 2, 0)
        assert attempt.observation.finish_reason == "STOP"

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["CONTINUATION", "MAX_TOKENS"])
def test_gemini_retains_native_incomplete_finish_reason_without_an_extra_send(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        return httpx2.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "部分回答"}]}, "finishReason": reason, "continuationToken": "Y29udGludWU="}]})

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                attempt = RequestAttempt(number=1, model=config.model, provider="google")
                result = await Agent(observe_request(model, attempt)).run("长回答")
        assert result.response.finish_reason == "length"
        assert attempt.observation.finish_reason == reason
        assert attempt.observation.usage_status == "unknown"
        assert len(sends) == 1

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
