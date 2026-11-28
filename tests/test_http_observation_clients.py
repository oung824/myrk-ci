import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from pydantic_ai import models

from myrk.conversation import Conversation, Task
from myrk.provider import ProviderConfig, configured_model
from tests.support.observed_http import observed_mock_transport


@pytest.fixture(params=["openai-chat", "google"])
def config(request: pytest.FixtureRequest) -> ProviderConfig:
    if request.param == "google":
        return ProviderConfig(name="Gemini", protocol="google", model="gemini-2.5-flash",
                              base_url="https://generativelanguage.googleapis.com")
    return ProviderConfig(name="OpenAI", protocol="openai-chat", model="gpt-4.1",
                          base_url="https://api.openai.com/v1")


def reply_body(config: ProviderConfig, text: str, *, input_tokens: int, output_tokens: int,
               finished: bool = True, report_thinking: bool = True) -> bytes:
    if config.protocol == "google":
        candidate = {"content": {"role": "model", "parts": [{"text": text}]}}
        if finished:
            candidate["finishReason"] = "STOP"
        usage = {"promptTokenCount": input_tokens, "candidatesTokenCount": output_tokens}
        if report_thinking:
            usage["thoughtsTokenCount"] = 0
        payload = {"candidates": [candidate], "usageMetadata": usage}
    else:
        payload = {
            "id": "controlled-response", "object": "chat.completion.chunk", "created": 1, "model": config.model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                         "finish_reason": "stop" if finished else None}],
            "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                      "total_tokens": input_tokens + output_tokens},
        }
    end = b"data: [DONE]\n\n" if finished and config.protocol != "google" else b""
    return f"data: {json.dumps(payload)}\n\n".encode() + end


def test_plain_external_client_retains_sdk_usage_and_request_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: ProviderConfig,
) -> None:
    sent_by = []
    hooked = []
    delivered = []

    class SuppliedClient(httpx2.AsyncClient):
        async def send(self, request, **kwargs):
            sent_by.append(self)
            return await super().send(request, **kwargs)

    async def record_request(request: httpx2.Request) -> None:
        hooked.append(request.content)

    def respond(request: httpx2.Request) -> httpx2.Response:
        delivered.append(request.content)
        return httpx2.Response(200,
            content=reply_body(config, "Completed.", input_tokens=12, output_tokens=4, report_thinking=False),
            headers={"content-type": "text/event-stream"})

    async def run() -> None:
        path = tmp_path / "plain.json"
        task = Task(goal="Answer with the supplied client.", scope=None, provider_config=config)
        async with SuppliedClient(transport=httpx2.MockTransport(respond),
                                  event_hooks={"request": [record_request]}) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(path).ask(task.goal, document=None, task=task, model=model)
            assert turn.status == "completed" and turn.answer == "Completed.", turn.error
            assert sent_by == [http] and hooked == delivered and len(delivered) == 1
            saved = Conversation.open(path).turns[0]
            assert len(saved.request_attempts) == saved.requests == 1
            attempt = saved.request_attempts[0]
            assert (attempt.input_tokens, attempt.output_tokens) == (12, 4)
            assert (saved.input_tokens, saved.output_tokens) == (12, 4)
            assert attempt.observation is not None and attempt.observation.wire is None
            assert attempt.observation.native_http is False
            assert http.event_hooks == {"request": [record_request], "response": []}

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_plain_external_client_preserves_measured_zero_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: ProviderConfig,
) -> None:
    async def run() -> None:
        path = tmp_path / "zero.json"
        task = Task(goal="Answer with zero reported usage.", scope=None, provider_config=config)
        transport = httpx2.MockTransport(lambda request: httpx2.Response(
            200, content=reply_body(config, "Completed.", input_tokens=0, output_tokens=0),
            headers={"content-type": "text/event-stream"},
        ))
        async with httpx2.AsyncClient(transport=transport) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(path).ask(task.goal, document=None, task=task, model=model)

        assert turn.status == "completed" and turn.answer == "Completed.", turn.error
        saved = Conversation.open(path).turns[0]
        attempt, = saved.request_attempts
        assert (saved.input_tokens, saved.output_tokens) == (0, 0)
        assert (attempt.input_tokens, attempt.output_tokens) == (0, 0)
        assert attempt.observation is not None and attempt.observation.native_http is False

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("content_type", [
    pytest.param("text/event-stream", id="standard"),
    pytest.param("Text/Event-Stream; charset=UTF-8", id="mixed-case"),
])
def test_observed_client_records_usage_from_multiline_sse_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: ProviderConfig, content_type: str,
) -> None:
    payload = reply_body(config, "Completed.", input_tokens=12, output_tokens=4).split(b"\n", 1)[0]
    split = payload.index(b",") + 1

    class MultilineReply(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield payload[:split] + b"\r"
            yield b"\ndata: " + payload[split:] + b"\r"
            yield b"\r"
            if config.protocol == "openai-chat":
                yield b"data: [DONE]\n\n"

    async def run() -> None:
        task = Task(goal="Read a multiline SSE event.", scope=None, provider_config=config)
        transport = observed_mock_transport(lambda request: httpx2.Response(
            200, stream=MultilineReply(), headers={"content-type": content_type},
        ))
        async with httpx2.AsyncClient(transport=transport) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(tmp_path / "multiline.json").ask(task.goal, document=None, task=task, model=model)

        assert turn.status == "completed" and turn.answer == "Completed.", turn.error
        attempt, = turn.request_attempts
        assert (attempt.input_tokens, attempt.output_tokens) == (12, 4)
        assert (turn.input_tokens, turn.output_tokens) == (12, 4)
        assert attempt.observation is not None and attempt.observation.native_http is True

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_response_hook_can_reject_a_buffered_body_before_usage_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: ProviderConfig,
) -> None:
    seen = []

    async def reject(response: httpx2.Response) -> None:
        seen.append((response.status_code, response.headers["content-type"]))
        raise RuntimeError("Rejected response without reading its body.")

    async def run() -> None:
        task = Task(goal="Reject the response in its hook.", scope=None, provider_config=config)
        transport = observed_mock_transport(lambda request: httpx2.Response(
            200, content=reply_body(config, "Unread.", input_tokens=12, output_tokens=4),
            headers={"content-type": "text/event-stream"},
        ))
        async with httpx2.AsyncClient(transport=transport, event_hooks={"response": [reject]}) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(tmp_path / "unread.json").ask(task.goal, document=None, task=task, model=model)

        assert turn.status == "failed" and seen == [(200, "text/event-stream")]
        attempt, = turn.request_attempts
        observation = attempt.observation
        assert observation is not None and observation.native_http is True
        assert observation.usage_present is False
        assert (observation.input_tokens, observation.output_tokens) == (None, None)
        assert observation.first_output_seconds is None

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_observed_transport_preserves_custom_response_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: ProviderConfig,
) -> None:
    reads = []

    class CustomResponse(httpx2.Response):
        async def aiter_bytes(self, chunk_size=None):
            reads.append("custom response read")
            async for chunk in super().aiter_bytes(chunk_size):
                yield chunk.replace(b"Original.", b"Custom.")

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield reply_body(config, "Original.", input_tokens=12, output_tokens=4)

    async def run() -> None:
        task = Task(goal="Use the transport's response reader.", scope=None, provider_config=config)
        transport = observed_mock_transport(lambda request: CustomResponse(
            200, stream=Stream(), headers={"content-type": "text/event-stream"},
        ))
        async with httpx2.AsyncClient(transport=transport) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(tmp_path / "custom.json").ask(task.goal, document=None, task=task, model=model)

        assert turn.status == "completed" and turn.answer == "Custom.", turn.error
        assert reads == ["custom response read"]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_cancelling_a_waiting_read_preserves_partial_output_and_closes_the_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: ProviderConfig,
) -> None:
    from pydantic_ai import CancellationToken

    from myrk.observed_http import ObservedHTTPTransport

    received = []
    events = []

    async def run() -> None:
        waiting = asyncio.Event()
        cancellation = CancellationToken()

        class PausedReply(httpx2.AsyncByteStream):
            async def __aiter__(self):
                events.append("read first event")
                yield reply_body(config, "Partial.", input_tokens=12, output_tokens=4, finished=False)
                events.append("waiting for more")
                waiting.set()
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                events.append("response closed")

        class ClosingTransport(httpx2.MockTransport):
            async def __aenter__(self):
                events.append("transport entered")
                return await super().__aenter__()

            async def aclose(self) -> None:
                events.append("transport closed")
                await super().aclose()

        def respond(request: httpx2.Request) -> httpx2.Response:
            received.append(request.content)
            return httpx2.Response(200, stream=PausedReply(), headers={"content-type": "text/event-stream"})

        def updated(update) -> None:
            if update.kind == "text":
                events.append(update.text)

        task = Task(goal="Keep the partial reply.", scope=None, provider_config=config)
        path = tmp_path / "cancelled.json"
        http = httpx2.AsyncClient(transport=ObservedHTTPTransport(ClosingTransport(respond)))
        await http.__aenter__()
        try:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                async with asyncio.timeout(20):
                    async with asyncio.TaskGroup() as group:
                        operation = group.create_task(Conversation(path).ask(
                            task.goal, document=None, task=task, model=model, on_update=updated,
                            cancellation_token=cancellation,
                        ))
                        await waiting.wait()
                        cancellation.cancel()
        finally:
            if not http.is_closed:
                await http.aclose()

        turn = operation.result()
        assert turn.status == "cancelled" and turn.answer == "Partial."
        assert len(received) == turn.requests == 1
        attempt = Conversation.open(path).turns[0].request_attempts[0]
        observation = attempt.observation
        assert observation is not None and observation.native_http is True
        assert observation.usage_status == "partial" and observation.finish_reason is None
        assert (observation.input_tokens, observation.output_tokens) == (12, 4)
        assert events == ["transport entered", "read first event", "Partial.", "waiting for more",
                          "response closed", "transport closed"]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_parallel_requests_on_one_client_keep_their_own_wire_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: ProviderConfig,
) -> None:
    from hashlib import sha256

    replies = {
        "CONTROLLED_OWNER_937": ("First response.", 12, 4),
        "CONTROLLED_OWNER_164": ("Second response.", 29, 7),
    }
    received = {}

    async def run() -> None:
        both_sent = asyncio.Barrier(2)

        async def respond(request: httpx2.Request) -> httpx2.Response:
            owner, = [owner for owner in replies if owner.encode() in request.content]
            received[owner] = request.content
            await both_sent.wait()
            answer, input_tokens, output_tokens = replies[owner]
            return httpx2.Response(200,
                content=reply_body(config, answer, input_tokens=input_tokens, output_tokens=output_tokens),
                headers={"content-type": "text/event-stream"})

        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                async def ask(owner: str):
                    task = Task(goal=owner, scope=None, provider_config=config)
                    return await Conversation(tmp_path / f"{owner}.json").ask(owner, document=None, task=task, model=model)

                async with asyncio.timeout(20):
                    async with asyncio.TaskGroup() as group:
                        operations = {owner: group.create_task(ask(owner)) for owner in replies}

        assert received.keys() == replies.keys()
        for owner, operation in operations.items():
            turn = operation.result()
            answer, input_tokens, output_tokens = replies[owner]
            assert turn.status == "completed" and turn.answer == answer, turn.error
            assert len(turn.request_attempts) == turn.requests == 1
            attempt = turn.request_attempts[0]
            assert (attempt.input_tokens, attempt.output_tokens) == (input_tokens, output_tokens)
            assert (turn.input_tokens, turn.output_tokens) == (input_tokens, output_tokens)
            observation = attempt.observation
            assert observation is not None and observation.native_http is True
            assert observation.wire is not None
            assert observation.wire.body_sha256 == sha256(received[owner]).hexdigest()

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
