import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from pydantic_ai import models

from myrk.conversation import Conversation, ConversationRecord, Task, TaskScope
from myrk.memory import MemoryStore
from myrk.provider import configured_model
from myrk.permissions import ApprovalSettings
from tests.support.gemini_responses import google_config, google_stream
from tests.support.observed_http import observed_mock_transport


@pytest.mark.parametrize("with_tool", [False, True])
@pytest.mark.parametrize("finish", [None, "SAFETY", "MALFORMED_FUNCTION_CALL", "OTHER", "FINISH_REASON_UNSPECIFIED", "FUTURE_UNKNOWN_REASON"])
def test_gemini_unsuccessful_finish_preserves_the_response_without_completing_or_executing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_tool: bool, finish: str | None) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    target = folder / "must-not-exist.txt"
    sends = []
    parts = [{"text": "尚未完整结束的内容"}]
    if with_tool:
        parts.append({"functionCall": {"id": "create", "name": "create_text", "args": {"path": str(target), "content": "must-not-be-written"}}, "thoughtSignature": "Y3JlYXRl"})

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        if len(sends) == 1:
            return google_stream(parts, finish, usage={"promptTokenCount": 12, "candidatesTokenCount": 5, "thoughtsTokenCount": 2})
        return google_stream([{"text": "不应继续请求。"}])

    async def run() -> None:
        config = google_config()
        scope = TaskScope(root=str(folder), allowed_actions=["edit"], provider="google", recipients=["google"])
        task = Task(goal=f"创建{target}", scope=scope, provider_config=config, approval=ApprovalSettings(mode="full_access"))
        path = tmp_path / "records" / "incomplete.json"
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(path).ask(task.goal, document=None, model=model, task=task, scope=scope)
        assert turn.status == "failed", turn.error
        assert turn.answer == "尚未完整结束的内容"
        assert turn.error and "结束原因" in turn.error
        assert not target.exists() and not turn.actions
        assert turn.delivery_status == "not_delivered"
        assert len(sends) == turn.requests == task.requests == 1
        assert (turn.input_tokens, turn.output_tokens) == (12, 7)
        attempt = turn.request_attempts[0]
        assert attempt.status == "failed"
        assert (attempt.input_tokens, attempt.output_tokens) == (12, 7)
        assert attempt.observation.finish_reason == finish
        assert attempt.observation.raw_usage == {"promptTokenCount": 12, "candidatesTokenCount": 5, "thoughtsTokenCount": 2}
        reopened = Conversation.open(path)
        saved = reopened.turns[-1]
        assert saved.status == "failed" and saved.answer == turn.answer
        assert (saved.request_attempts[0].input_tokens, saved.request_attempts[0].output_tokens) == (12, 7)
        response = ConversationRecord.model_validate_json(path.read_text(encoding="utf-8")).messages[-1]
        assert response.kind == "response"
        assert response.parts[0].content == "尚未完整结束的内容"
        assert (response.provider_details or {}).get("finish_reason") == finish
        if with_tool:
            assert response.parts[1].tool_name == "create_text"
            assert response.parts[1].provider_details["thought_signature"] == "Y3JlYXRl"

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_gemini_memory_review_without_a_finish_reason_cannot_save_its_proposed_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        if len(sends) == 1:
            return google_stream([{"text": "主要工作已交付。"}])
        if len(sends) == 2:
            return google_stream([{"functionCall": {"id": "note", "name": "memory", "args": {"action": "add", "text": "不应从未完成回应保存的笔记。"}}}], None,
                                 usage={"promptTokenCount": 12, "candidatesTokenCount": 5, "thoughtsTokenCount": 2})
        return google_stream([{"text": "不应继续复盘。"}])

    async def run() -> None:
        config = google_config()
        path = tmp_path / "records" / "memory-review.json"
        task = Task(goal="交付后整理经验", scope=None, provider_config=config, memory_review_requested=True)
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(path).ask(task.goal, document=None, model=model, task=task)
        assert turn.status == "completed" and turn.answer == "主要工作已交付。"
        assert not turn.actions
        assert MemoryStore(path.parent / "memories").read().facts == []
        assert len(sends) == task.requests == 2
        assert any("有限复盘未完成" in note for note in turn.context_notes)
        saved = Conversation.open(path).turns[-1]
        attempt = saved.request_attempts[-1]
        assert attempt.actor == "memory_review" and attempt.status == "failed"
        assert (attempt.input_tokens, attempt.output_tokens) == (12, 7)
        assert saved.memory_review_messages[-1].parts[0].tool_name == "memory"

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("finish", ["CONTINUATION", "MAX_TOKENS"])
def test_gemini_truncated_answer_is_preserved_without_claiming_completion_or_sending_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str) -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        return google_stream([{"text": "还没说完的回答"}], finish)

    async def run() -> None:
        config = google_config()
        path = tmp_path / "truncated.json"
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(path).ask("长回答", document=None, model=model, task=Task(goal="长回答", scope=None, provider_config=config))
        assert turn.status == "limited", turn.error
        assert turn.answer == "还没说完的回答"
        assert turn.error and "未完整结束" in turn.error
        assert turn.delivery_status == "not_delivered"
        assert len(sends) == turn.requests == turn.task.requests == 1
        saved = Conversation.open(path).turns[-1]
        assert saved.status == "limited" and saved.answer == turn.answer

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("finish", ["MAX_TOKENS", None, "SAFETY"])
def test_gemini_incomplete_review_cannot_approve_a_write_even_when_its_partial_json_is_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str | None) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    target = tmp_path / "must-not-exist.txt"
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        if len(sends) == 1:
            return google_stream([{"functionCall": {"id": "create", "name": "create_text", "args": {"path": str(target), "content": "truncated-review"}}, "thoughtSignature": "Y3JlYXRl"}])
        if len(sends) == 2:
            return google_stream([{"text": json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "用户明确要求这份小文件，范围清楚且没有覆盖原文件。", "same_denied_outcome": False})}], finish,
                                 usage={"promptTokenCount": 12, "candidatesTokenCount": 5, "thoughtsTokenCount": 2})
        return google_stream([{"text": "本轮结束。"}])

    async def run() -> None:
        config = google_config()
        scope = TaskScope(root=str(folder), allowed_actions=["edit"], provider="google", recipients=["google"])
        task = Task(goal=f"创建指定文件{target}", scope=scope, provider_config=config, approval=ApprovalSettings(mode="auto_review"))
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(tmp_path / "records" / "review.json").ask(task.goal, document=None, model=model, task=task, scope=scope)
        assert not target.exists()
        action = turn.actions[0]
        assert action.review.status == "failed"
        assert action.denial_source == "review_incomplete"
        assert action.review.attempts == task.review_requests == 1
        assert len(sends) == task.requests == 3
        assert (action.review.input_tokens, action.review.output_tokens) == (12, 7)
        attempt = action.review.request_attempts[0]
        assert attempt.status == ("completed" if finish == "MAX_TOKENS" else "failed")
        assert attempt.observation.finish_reason == finish
        response = action.review.messages[-1]
        assert response.kind == "response"
        assert json.loads(response.parts[0].content)["decision"] == "allow"
        saved_review = Conversation.open(tmp_path / "records" / "review.json").turns[-1].actions[0].review
        assert saved_review.request_attempts[0].observation.finish_reason == finish
        assert saved_review.messages == action.review.messages

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
