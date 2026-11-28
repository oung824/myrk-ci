import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from pydantic_ai import models

from myrk.conversation import Conversation, Task, TaskScope
from myrk.provider import ProviderConfig, configured_model
from myrk.permissions import ApprovalSettings
from tests.support.gemini_responses import google_config, google_stream


@pytest.mark.parametrize("model_name,thinking,expected", [
    ("gemini-2.5-flash", False, {"thinking_budget": 0}),
    ("gemini-2.5-pro", False, {"thinking_budget": 128}),
    ("gemini-3-flash-preview", False, {"thinking_level": "MINIMAL"}),
    ("gemini-3-pro-preview", False, {"thinking_level": "LOW"}),
    ("gemini-2.5-flash", True, {"include_thoughts": True}),
    ("gemini-3-pro-preview", True, {"include_thoughts": True}),
])
def test_gemini_thinking_preference_uses_the_sdk_model_capability_in_the_actual_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_name: str, thinking: bool, expected: dict) -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(json.loads(request.content))
        return google_stream([{"text": "收到。"}])

    async def run() -> None:
        config = google_config(model_name)
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(tmp_path / "thinking.json").ask("简单问候", document=None, model=model, task=Task(goal="简单问候", scope=None, provider_config=config), thinking=thinking)
        assert turn.status == "completed", turn.error
        assert sends[0]["generationConfig"]["thinkingConfig"] == expected

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("target", [google_config("gemini-2.5-pro"), ProviderConfig()])
def test_signed_gemini_history_refuses_an_incompatible_model_or_service_without_sending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: ProviderConfig) -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        if len(sends) == 1:
            return google_stream([{"functionCall": {"id": "read", "name": "read_text", "args": {"path": "source.txt"}}, "thoughtSignature": "cHJlc2VydmU="}])
        return httpx2.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "controlled stop"}})

    async def run() -> None:
        folder = tmp_path / "materials"
        folder.mkdir()
        (folder / "source.txt").write_text("保留历史", encoding="utf-8")
        config = google_config()
        scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="google", recipients=["google"])
        task = Task(goal="保留历史", scope=scope, provider_config=config)
        path = tmp_path / "records" / "signed.json"
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(path).ask(task.goal, document=None, model=model, task=task, scope=scope)
        assert turn.status == "failed" and turn.service_failure
        reopened = Conversation.open(path)
        preview = reopened.preview_provider_change(task.id, target)
        assert not preview.compatible
        with pytest.raises(ValueError, match="附带|签名"):
            reopened.decide_provider_change(preview.id, True)
        assert reopened.turns[-1].task.provider_config == config
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", target, http_client=http) as model:
                with pytest.raises(ValueError, match="型号.*不符"):
                    await reopened.resume_task(task.id, model=model)
        assert len(sends) == task.requests == 2

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_gemini_parallel_and_consecutive_tools_keep_signed_empty_parts_when_reopened_and_resumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "first.txt").write_text("第一个完整结果", encoding="utf-8")
    (folder / "second.txt").write_text("第二个完整结果", encoding="utf-8")
    sends = []
    signed_parallel = [
        {"text": "保留非空思考", "thought": True, "thoughtSignature": "bm9uZW1wdHk="},
        {"text": "", "thought": True, "thoughtSignature": "dGhvdWdodA=="},
        {"functionCall": {"id": "first", "name": "read_text", "args": {"path": "first.txt"}}, "thoughtSignature": "Zmlyc3Q="},
        {"functionCall": {"id": "second", "name": "read_text", "args": {"path": "second.txt"}}, "thoughtSignature": "c2Vjb25k"},
        {"text": "", "thoughtSignature": "ZW1wdHk="},
    ]
    signed_consecutive = [{"functionCall": {"id": "again", "name": "read_text", "args": {"path": "first.txt"}}, "thoughtSignature": "YWdhaW4="}]

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(json.loads(request.content))
        if len(sends) == 1:
            return google_stream(signed_parallel)
        if len(sends) == 2:
            return google_stream(signed_consecutive)
        if len(sends) == 3:
            return httpx2.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "controlled stop"}})
        return google_stream([{"text": "已沿用原结果继续，无需重做读取。"}])

    async def run() -> None:
        config = google_config()
        scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="google", recipients=["google"])
        task = Task(goal="按完整资料继续", scope=scope, provider_config=config)
        path = tmp_path / "records" / "tools.json"
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                first = await Conversation(path).ask(task.goal, document=None, model=model, task=task, scope=scope)
        assert first.status == "failed" and first.service_failure, first.error
        assert len(first.actions) == 3 and all(action.status == "completed" for action in first.actions)
        assert len(sends) == first.requests == task.requests == 3

        reopened = Conversation.open(path)
        assert reopened.recovery is not None and reopened.recovery.can_resume
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                resumed = await reopened.resume_task(task.id, model=model)
        assert resumed.status == "completed", resumed.error
        assert not resumed.actions
        assert resumed.task.requests == len(sends) == 4
        assert resumed.requests == 1
        assert [content["parts"] for content in sends[-1]["contents"] if content["role"] == "model"] == [signed_parallel, signed_consecutive]
        returns = [part["functionResponse"] for content in sends[-1]["contents"] for part in content["parts"] if "functionResponse" in part]
        assert {part["id"] for part in returns} == {"first", "second", "again"}
        assert "第一个完整结果" in json.dumps(returns, ensure_ascii=False)
        assert "第二个完整结果" in json.dumps(returns, ensure_ascii=False)
        assert "myrk-empty-thought-" not in path.read_text(encoding="utf-8")
        assert "myrk-empty-thought-" not in json.dumps(sends)

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("with_tool", [False, True])
def test_gemini_signed_text_parts_stay_separate_from_each_other_and_unsigned_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_tool: bool) -> None:
    sends = []
    updates = []
    signed_parts = [
        {"text": "前文"},
        {"text": "签名文字", "thoughtSignature": "c2lnbmVk"},
        {"text": "", "thoughtSignature": "ZW1wdHk="},
        {"text": "后文"},
    ]
    if with_tool:
        signed_parts.append({"functionCall": {"id": "read", "name": "read_text", "args": {"path": "source.txt"}}, "thoughtSignature": "dG9vbA=="})

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(json.loads(request.content))
        return google_stream(signed_parts if len(sends) == 1 else [{"text": "完成。"}])

    async def run() -> None:
        folder = tmp_path / "materials"
        folder.mkdir()
        (folder / "source.txt").write_text("真实读取结果", encoding="utf-8")
        config = google_config()
        scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="google", recipients=["google"])
        task = Task(goal="完整保留文字部件", scope=scope, provider_config=config)
        path = tmp_path / "records" / "text-parts.json"
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(path).ask(task.goal, document=None, model=model, task=task, scope=scope, on_update=updates.append)
                if not with_tool:
                    assert turn.status == "completed" and turn.answer == "前文签名文字后文"
                    turn = await Conversation.open(path).ask("继续", document=None, model=model, task=task, scope=scope, on_update=updates.append)
        assert turn.status == "completed", turn.error
        assert [content["parts"] for content in sends[-1]["contents"] if content["role"] == "model"] == [signed_parts]
        assert "".join(update.text for update in updates if update.kind == "text") == "前文签名文字后文完成。"

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


def test_gemini_child_uses_independent_review_and_keeps_its_own_round_and_request_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "notes.txt").write_text("主助理核对的完整成果", encoding="utf-8")
    target = tmp_path / "approved.txt"
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sends.append(body)
        if len(sends) == 1:
            return google_stream([{"functionCall": {"id": "delegate", "name": "delegate_work", "args": {"goal": "创建明确指定的小文件", "context": f"用户明确允许创建{target}，只写approved-text；成果notes.txt保持原文", "evidence_requirements": "真实动作及成果全文", "expected_files": ["notes.txt"]}}, "thoughtSignature": "ZGVsZWdhdGU="}])
        if len(sends) == 2:
            return google_stream([{"functionCall": {"id": "create", "name": "create_text", "args": {"path": str(target), "content": "approved-text"}}, "thoughtSignature": "Y3JlYXRl"}])
        if len(sends) == 3:
            names = {tool["name"] for group in body["tools"] for tool in group.get("functionDeclarations", [])}
            assert names == {"read_text", "run_command"}
            assert "独立动作审核者" in json.dumps(body, ensure_ascii=False)
            assert not any(part.get("thought") for content in body["contents"] for part in content["parts"])
            assert not target.exists()
            return google_stream([{"text": json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "用户明确允许创建这个指定的小文件，不覆盖原有成果。", "same_denied_outcome": False})}])
        if len(sends) == 4:
            return google_stream([{"text": "指定小文件已创建；成果notes.txt完整原文是主助理核对的完整成果。"}])
        if len(sends) == 5:
            delegated = next(part["functionResponse"] for content in body["contents"] for part in content["parts"] if part.get("functionResponse", {}).get("name") == "delegate_work")
            work = json.loads(delegated["response"]["return_value"])
            return google_stream([{"functionCall": {"id": "verify", "name": "verify_delegation", "args": {"delegation_id": work["id"], "path": "notes.txt", "expected_text": "主助理核对的完整成果"}}, "thoughtSignature": "dmVyaWZ5"}])
        return google_stream([{"text": "分工成果已核对，等待用户验收。"}])

    async def run() -> None:
        config = google_config()
        scope = TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="google", recipients=["google"])
        task = Task(goal=f"分工创建{target}并由主助理核验已有成果", scope=scope, provider_config=config, delegation_max_turns=5, approval=ApprovalSettings(mode="auto_review"))
        conversation = Conversation(tmp_path / "records" / "delegation.json")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await conversation.ask(task.goal, document=None, model=model, task=task, scope=scope)
        assert turn.status == "completed", turn.error
        assert target.read_text(encoding="utf-8") == "approved-text"
        work = turn.delegations[0]
        assert work.verification_status == "passed" and work.status == "completed"
        assert task.requests == task.turns_used == 3
        assert task.review_requests == 0
        assert work.requests == work.turns_used == 3
        assert work.review_requests == 1
        assert task.total_requests == len(sends) == 6
        child = Conversation.open(Path(work.record_path)).turns[-1]
        assert child.request_owner_id == work.id
        assert child.request_account.requests == 3 and child.request_account.review_requests == 1
        created = next(action for action in child.actions if action.kind == "create")
        assert created.approval_basis == "assistant" and created.review.status == "allowed"
        assert created.review.request_owner_id == work.id
        numbers = [attempt.number for attempt in turn.request_attempts] + [attempt.number for attempt in child.request_attempts] + [attempt.number for attempt in created.review.request_attempts]
        assert sorted(numbers) == [1, 2, 3, 4, 5, 6]
        with pytest.raises(ValueError, match="子助理"):
            Conversation.open(Path(work.record_path)).prepare_resume(task.id)

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("approved", [False, True])
def test_gemini_write_waits_for_the_existing_user_approval_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approved: bool) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    target = tmp_path / "user-approved.txt"
    sends = []
    decisions = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(json.loads(request.content))
        if len(sends) == 1:
            return google_stream([{"functionCall": {"id": "create", "name": "create_text", "args": {"path": str(target), "content": "user-approved"}}, "thoughtSignature": "YXBwcm92YWw="}])
        return google_stream([{"text": "已按本人的批准决定结束本轮。"}])

    async def decide(action) -> bool:
        decisions.append(action)
        assert action.status == "awaiting_approval" and not target.exists()
        return approved

    async def run() -> None:
        config = google_config()
        scope = TaskScope(root=str(folder), allowed_actions=["edit"], provider="google", recipients=["google"])
        task = Task(goal=f"创建指定文件{target}", scope=scope, provider_config=config)
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(tmp_path / "records" / "approval.json").ask(task.goal, document=None, model=model, task=task, scope=scope, approve_action=decide)
        assert turn.status == "completed", turn.error
        assert len(decisions) == 1
        action = turn.actions[0]
        assert action.status == ("completed" if approved else "denied")
        assert (action.approval_basis == "user") if approved else action.denial_source == "user"
        assert target.exists() == approved
        if approved:
            assert target.read_text(encoding="utf-8") == "user-approved"
        assert task.review_requests == 0
        assert len(sends) == turn.requests == task.requests == 2

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())
