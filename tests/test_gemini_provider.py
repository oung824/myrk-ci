import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from pydantic_ai import models

from myrk.conversation import Conversation, QueuedTask, Task, TaskScope
from myrk.provider import GoogleCatalogConnection, ProviderConfig, configured_model, fetch_model_catalog


def test_gemini_recipient_and_model_survive_saved_work(tmp_path: Path) -> None:
    config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-example", protocol="google")
    conversation = Conversation(tmp_path / "gemini.json")
    conversation.queue.append(QueuedTask(task=Task(goal="文字对话", scope=TaskScope(root=str(tmp_path), allowed_actions=[], provider="google", recipients=["google"]), provider_config=config), document=None))
    conversation.save()

    reopened = Conversation.open(conversation.path)
    assert reopened.queue[0].task.provider_config == config
    assert reopened.queue[0].task.scope is not None
    assert reopened.queue[0].task.scope.recipients == ["google"]


def test_gemini_factory_streams_text_and_counts_the_actual_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sends = []
    updates = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        chunks = [
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "你好"}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "，世界"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5}},
        ]
        return httpx2.Response(200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks), headers={"content-type": "text/event-stream"})

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(tmp_path / "text.json").ask("你好", document=None, model=model, task=Task(goal="你好", scope=None, provider_config=config), on_update=updates.append)
        assert turn.status == "completed", turn.error
        assert turn.answer == "你好，世界"
        assert len(sends) == turn.requests == turn.task.requests == len(turn.request_attempts) == 1
        assert sends[0].url.host == "generativelanguage.googleapis.com"
        assert sends[0].url.path.endswith("models/gemini-2.5-flash:streamGenerateContent")
        assert any(update.kind == "text" and update.text == "你好" for update in updates)
        assert "synthetic-only" not in (tmp_path / "text.json").read_text(encoding="utf-8")

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_gemini_does_not_send_hidden_continuations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": "尚未完结"}]}, "finishReason": "CONTINUATION" if len(sends) == 1 else "STOP", "continuationToken": "Y29udGludWU="}]}
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\n", headers={"content-type": "text/event-stream"})

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(tmp_path / "continuation.json").ask("长回答", document=None, model=model, task=Task(goal="长回答", scope=None, provider_config=config))
        assert len(sends) == turn.requests == turn.task.requests == len(turn.request_attempts) == 1

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_gemini_catalog_reads_every_page_and_keeps_capability_unknown() -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        if len(sends) == 1:
            return httpx2.Response(200, json={"models": [{"name": "models/gemini-first", "displayName": "第一个型号", "supportedGenerationMethods": ["generateContent"]}, {"name": "models/embed-only", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "second"})
        return httpx2.Response(200, json={"models": [{"name": "models/gemini-later", "supportedGenerationMethods": ["generateContent"]}, {"name": "models/unknown-capability"}]})

    async def run() -> None:
        connection = GoogleCatalogConnection(name="Gemini", base_url="https://generativelanguage.googleapis.com", protocol="google")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            result = await fetch_model_catalog("synthetic-only", connection, http_client=http)
        assert [entry.model_dump() for entry in result] == [
            {"id": "gemini-first", "name": "第一个型号", "capability": "unknown"},
            {"id": "gemini-later", "name": "gemini-later", "capability": "unknown"},
            {"id": "unknown-capability", "name": "unknown-capability", "capability": "unknown"},
        ]
        assert len(sends) == 2 and sends[1].url.params["pageToken"] == "second"

    asyncio.run(run())


@pytest.mark.parametrize("status,label", [(401, "认证失败"), (403, "权限不足"), (429, "额度限制"), (503, "服务暂不可用")])
@pytest.mark.parametrize("later_page", [False, True])
def test_gemini_catalog_reports_errors_without_returning_partial_or_empty_success(status: int, label: str, later_page: bool) -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        if later_page and len(sends) == 1:
            return httpx2.Response(200, json={"models": [{"name": "models/first", "supportedGenerationMethods": ["generateContent"]}], "nextPageToken": "later"})
        return httpx2.Response(status, json={"error": {"code": status, "status": "CONTROLLED", "message": "synthetic-only rejected"}})

    async def run() -> None:
        connection = GoogleCatalogConnection(name="Gemini", base_url="https://generativelanguage.googleapis.com", protocol="google")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            with pytest.raises(ValueError, match=label) as caught:
                await fetch_model_catalog("synthetic-only", connection, http_client=http)
        assert ("后续分页" in str(caught.value)) == later_page
        assert "synthetic-only" not in str(caught.value)
        assert len(sends) == (2 if later_page else 1)

    asyncio.run(run())


def test_gemini_catalog_distinguishes_empty_success_from_network_failure() -> None:
    async def run() -> None:
        connection = GoogleCatalogConnection(name="Gemini", base_url="https://generativelanguage.googleapis.com", protocol="google")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={"models": []}))) as http:
            assert await fetch_model_catalog("synthetic-only", connection, http_client=http) == []

        def disconnected(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("synthetic-only offline", request=request)

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(disconnected)) as http:
            with pytest.raises(ValueError, match="网络连接失败") as caught:
                await fetch_model_catalog("synthetic-only", connection, http_client=http)
        assert "synthetic-only" not in str(caught.value)

    asyncio.run(run())


@pytest.mark.parametrize("status,expected", [(503, 3), (401, 1), (403, 1), (429, 3)])
def test_gemini_service_errors_count_each_app_attempt_without_hidden_sdk_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, expected: int) -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        return httpx2.Response(status, json={"error": {"code": status, "status": "RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE", "message": "controlled failure"}})

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                task = Task(goal="保留失败工作", scope=None, provider_config=config, request_limit=10)
                turn = await Conversation(tmp_path / "failure.json").ask(task.goal, document=None, task=task, model=model)
        assert turn.status == "failed" and turn.service_failure
        assert len(sends) == turn.requests == task.requests == len(turn.request_attempts) == expected
        assert [attempt.http_status for attempt in turn.request_attempts] == [status] * expected

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
