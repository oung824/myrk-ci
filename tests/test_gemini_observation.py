import asyncio
from hashlib import sha256
import json
from pathlib import Path

import httpx2
import pytest
from pydantic_ai import Agent, CancellationToken, models

from myrk.conversation import Conversation, Task, TaskScope
from myrk.provider import ProviderConfig, configured_model
from myrk.request_observation import observe_request
from myrk.task_control import RequestAttempt
from tests.support.observed_http import observed_mock_transport


def test_gemini_final_exchange_preserves_native_usage_and_request_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sends = []
    usage = {"promptTokenCount": 100, "candidatesTokenCount": 5, "thoughtsTokenCount": 7,
             "cachedContentTokenCount": 80, "totalTokenCount": 112}

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        chunks = [
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "核对", "thought": True}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "完成"}]}, "finishReason": "STOP"}], "usageMetadata": usage},
        ]
        return httpx2.Response(200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks), headers={"content-type": "text/event-stream"})

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        path = tmp_path / "observed.json"
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(path).ask("回答", document=None, model=model, task=Task(goal="回答", scope=None, provider_config=config))
        assert turn.status == "completed", turn.error
        saved = Conversation.open(path).turns[0]
        attempt = saved.request_attempts[0]
        observation = attempt.observation
        assert observation is not None and observation.protocol == "google"
        assert (observation.input_tokens, observation.output_tokens, observation.thinking_tokens, observation.cache_read_tokens, observation.cache_write_tokens) == (100, 12, 7, 80, None)
        assert observation.raw_usage == usage
        assert observation.field_presence == {"promptTokenCount": True, "candidatesTokenCount": True, "thoughtsTokenCount": True, "cachedContentTokenCount": True, "totalTokenCount": True, "toolUsePromptTokenCount": False}
        assert observation.input_source == "usageMetadata.promptTokenCount"
        assert observation.output_source == "usageMetadata.candidatesTokenCount + usageMetadata.thoughtsTokenCount"
        assert observation.thinking_source == "usageMetadata.thoughtsTokenCount"
        assert observation.cache_read_source == "usageMetadata.cachedContentTokenCount"
        assert observation.finish_reason == "STOP"
        assert observation.sdk_usage["output_tokens"] == 12
        assert (saved.input_tokens, saved.output_tokens) == (100, 12)
        assert observation.first_output_seconds is not None and observation.elapsed_seconds >= observation.first_output_seconds
        assert observation.wire is not None
        payload = json.loads(sends[0].content)
        assert observation.wire.body_sha256 == sha256(sends[0].content).hexdigest()
        assert observation.wire.message_count == len(payload["contents"])
        assert observation.wire.tool_count == sum(len(tool.get("functionDeclarations", [])) for tool in payload["tools"])
        assert observation.wire.system_sha256 != observation.wire.messages_sha256
        assert len(sends) == saved.requests == saved.task.requests == len(saved.request_attempts) == 1
        assert "synthetic-only" not in path.read_text(encoding="utf-8")
        assert "回答" not in observation.model_dump_json()

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("stopped", [False, True])
def test_gemini_interrupted_stream_keeps_partial_output_and_known_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: bool) -> None:
    sends = []
    delivered = []
    token = CancellationToken()

    class Interrupted(httpx2.AsyncByteStream):
        async def __aiter__(self):
            chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": "部分回答"}]}}],
                     "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 2, "thoughtsTokenCount": 3, "cachedContentTokenCount": 80}}
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            assert "部分回答" in delivered, "观测不得预读下一块而阻塞首段输出"
            if stopped:
                await asyncio.Event().wait()
            raise httpx2.ReadError("controlled stream interruption")

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        return httpx2.Response(200, stream=Interrupted(), headers={"content-type": "text/event-stream"})

    def updated(update) -> None:
        if update.kind == "text":
            delivered.append(update.text)
            if stopped:
                token.cancel()

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        path = tmp_path / "interrupted.json"
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(path).ask("回答", document=None, model=model, task=Task(goal="回答", scope=None, provider_config=config), on_update=updated, cancellation_token=token)
        saved = Conversation.open(path).turns[0]
        assert turn.status == saved.status == ("cancelled" if stopped else "failed")
        assert saved.answer == "部分回答"
        assert len(sends) == saved.requests == saved.task.requests == len(saved.request_attempts) == 1
        attempt = saved.request_attempts[0]
        assert attempt.status == "failed"
        if stopped:
            assert "停止" in attempt.decision
        observation = attempt.observation
        assert observation is not None
        assert (observation.input_tokens, observation.output_tokens, observation.thinking_tokens, observation.cache_read_tokens) == (100, 5, 3, 80)
        assert observation.finish_reason is None and observation.usage_status == "partial"
        assert observation.first_output_seconds is not None and observation.elapsed_seconds >= observation.first_output_seconds

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_gemini_tool_steps_and_reopening_preserve_the_final_request_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sends = []
    (tmp_path / "note.txt").write_text("已授权的原文", encoding="utf-8")

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(json.loads(request.content))
        part = {"functionCall": {"name": "read_text", "args": {"path": "note.txt"}}} if len(sends) == 1 else {"text": "完成"}
        chunk = {"candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP"}],
                 "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 2, "thoughtsTokenCount": 0, "cachedContentTokenCount": 0}}
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\n", headers={"content-type": "text/event-stream"})

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        path = tmp_path / "layout.json"
        scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="google", recipients=["google"])
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                first = await Conversation(path).ask("读取原文", document=None, scope=scope, model=model, task=Task(goal="读取原文", scope=scope, provider_config=config))
                assert first.status == "completed", first.error
                assert first.actions[0].status == "completed", first.actions[0].model_dump()
                reopened = Conversation.open(path)
                second = await reopened.ask("继续", document=None, scope=scope, model=model, task=reopened.turns[-1].task)
                assert second.status == "completed", second.error
        assert len(sends) == second.task.requests == second.task.total_requests == 3
        observations = [attempt.observation for turn in Conversation.open(path).turns for attempt in turn.request_attempts]
        assert all(observation is not None and observation.first_output_seconds is not None for observation in observations)
        assert observations[0].wire.change_reasons == ["new_task"]
        assert observations[1].wire.change_reasons == observations[2].wire.change_reasons == []
        assert len({observation.wire.system_sha256 for observation in observations}) == 1
        assert len({observation.wire.tools_sha256 for observation in observations}) == 1
        for previous, current in zip(sends, sends[1:]):
            assert current["systemInstruction"] == previous["systemInstruction"]
            assert current["tools"] == previous["tools"]
            assert current["contents"][:len(previous["contents"])] == previous["contents"]
        assert any(part.get("functionResponse", {}).get("name") == "read_text" for message in sends[1]["contents"] for part in message["parts"])

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_gemini_failed_send_and_last_turn_preserve_ledger_and_unknown_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(json.loads(request.content))
        if len(sends) == 1:
            return httpx2.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE", "message": "controlled failure"}})
        chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": "完成"}]}, "finishReason": "STOP"}],
                 "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "thoughtsTokenCount": 0, "cachedContentTokenCount": 0}}
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\n", headers={"content-type": "text/event-stream"})

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        path = tmp_path / "retry.json"
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(path).ask("最后一轮", document=None, model=model, task=Task(goal="最后一轮", scope=None, provider_config=config, max_turns=1))
        saved = Conversation.open(path).turns[0]
        assert saved.status == turn.status == "completed"
        assert len(sends) == saved.requests == saved.task.requests == len(saved.request_attempts) == 2
        failed, completed = saved.request_attempts
        assert failed.status == "failed" and failed.http_status == 503
        assert failed.observation.usage_status == "unknown" and not failed.observation.usage_present
        assert failed.observation.first_output_seconds is None and failed.observation.finish_reason is None
        assert completed.status == "completed" and completed.observation.finish_reason == "STOP"
        assert completed.observation.cache_read_tokens == 0
        assert all(attempt.observation.wire.tool_count == 0 for attempt in saved.request_attempts)
        assert all(not payload.get("tools") for payload in sends)
        record = json.loads(path.read_text(encoding="utf-8"))
        for item in record["turns"][0]["request_attempts"]:
            for name in ("thinking_tokens", "thinking_source", "finish_reason"):
                del item["observation"][name]
        path.write_text(json.dumps(record), encoding="utf-8")
        old = Conversation.open(path).turns[0]
        assert all(attempt.observation.thinking_tokens is None and attempt.observation.finish_reason is None for attempt in old.request_attempts)
        assert old.task.requests == 2

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_gemini_rebinding_preserves_request_bytes_and_each_attempt_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request.content)
        return httpx2.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "完成"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": len(sends) * 100, "candidatesTokenCount": 2, "thoughtsTokenCount": 0}})

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                first = RequestAttempt(number=1, model=config.model, provider="google")
                second = RequestAttempt(number=2, model=config.model, provider="google")
                observed = observe_request(model, first)
                await Agent(observed).run("相同输入")
                await Agent(observe_request(observed, second)).run("相同输入")
                await Agent(model).run("相同输入")
        assert sends[0] == sends[1] == sends[2]
        assert (first.observation.input_tokens, second.observation.input_tokens) == (100, 200)
        assert first.observation.wire.body_sha256 == second.observation.wire.body_sha256

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
