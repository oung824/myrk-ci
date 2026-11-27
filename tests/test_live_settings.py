"""Approval choices through public conversation and desktop message entry points."""

from pathlib import Path
import asyncio
import json

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.function import DeltaToolCall
from pydantic_ai.messages import ToolReturnPart, UserPromptPart

from myrk.conversation import Conversation, Task, TaskScope

from tests.test_desktop_service import connection


def test_chat_choice_is_saved_without_secrets_and_global_default_only_initializes_new_chats(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "new"})
        original = channel.receive()["path"]
        channel.send({"action": "set_conversation_approval", "path": original, "approval_mode": "full_access"})
        selected = channel.receive()
        assert selected["event"] == "conversation_approval" and selected["saved"] is True
        assert selected["approval_settings"]["mode"] == "full_access"
        channel.send({"action": "configure", "approval_mode": "auto_review"})
        assert channel.receive()["configured"] is False
        channel.send({"action": "snapshot"})
        snapshot = channel.receive()
        existing = next(item for item in snapshot["conversations"] if item["path"] == original)
        assert existing["approval_settings"]["mode"] == "full_access"
        channel.send({"action": "new"})
        following = channel.receive()
        assert following["approval_settings"]["mode"] == "auto_review"
    with connection(tmp_path) as channel:
        channel.send({"action": "open", "path": original})
        restored = channel.receive()
        assert restored["event"] == "conversation"
        assert restored["approval_settings"] == selected["approval_settings"]


def test_new_turn_override_is_saved_for_later_turns_without_rewriting_previous_turns(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-settings"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "第一轮", "approval_mode": "auto_review"})
        while (first := channel.receive())["event"] != "finished":
            pass
        assert first["turn"]["task"]["approval"]["mode"] == "auto_review"
        channel.send({"action": "send", "prompt": "第二轮"})
        while (second := channel.receive())["event"] != "finished":
            pass
        assert second["turn"]["task"]["approval"]["mode"] == "auto_review"
        channel.send({"action": "set_conversation_approval", "approval_mode": "full_access"})
        assert channel.receive()["event"] == "conversation_approval"
        channel.send({"action": "snapshot"})
        saved = channel.receive()["conversations"][0]
        assert saved["approval_settings"]["mode"] == "full_access"
        assert [turn["task"]["approval"]["mode"] for turn in saved["turns"]] == ["auto_review", "auto_review"]


def test_explicit_same_label_upgrades_legacy_choice_only_for_future_work(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"version": 4, "messages": [], "turns": [{"prompt": "旧工作", "answer": "", "status": "cancelled",
        "task": {"id": "legacy-task", "goal": "旧工作", "request_limit": 10,
                 "scope": {"root": str(tmp_path), "allowed_actions": ["read"], "provider": "deepseek", "recipients": ["deepseek"]}}}]}), encoding="utf-8")
    original = path.read_bytes()
    conversation = Conversation.open(path)
    assert conversation.approval_settings.access.legacy_scope is True
    conversation.set_approval_mode("request_approval")
    assert conversation.turns[0].task.approval.access.legacy_scope is True

    async def model(_messages, _info):
        yield "按新选择继续。"
    async def run():
        turn = await Conversation.open(path).ask("明确切换后新工作", document=None, model=FunctionModel(stream_function=model), new_task=True)
        assert turn.task.approval.mode == "request_approval"
        assert turn.task.approval.access.legacy_scope is False
    asyncio.run(run())
    assert next((tmp_path / "backups").glob("legacy-v4-*.json")).read_bytes() == original


def test_live_reviewer_change_keeps_pending_action_exact_and_only_affects_later_actions(tmp_path: Path) -> None:
    folder = tmp_path / "task"
    folder.mkdir()
    conversation = Conversation(tmp_path / "records" / "session.json")
    task = Task(goal="依次创建两个明确指定的小文件", scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]), request_limit=10)
    pending = []
    async def approve(action):
        pending.append(action)
        captured = action.approval_settings.model_dump()
        changed = conversation.set_turn_reviewer(task.id, "assistant")
        assert changed.model_dump(exclude={"reviewer"}) == action.approval_settings.model_dump(exclude={"reviewer"})
        assert action.approval_settings.model_dump() == captured
        conversation.set_approval_mode("full_access")
        return True
    async def main(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) < 2:
            yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": str(tmp_path / f"outside-{len(results)}.txt"), "content": f"result-{len(results)}"}), tool_call_id=f"create-{len(results)}")}
        else:
            yield "两个具体文件已创建。"
    async def reviewer(_messages, _info):
        yield json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "只创建已明确要求的小文件，不覆盖内容。", "same_denied_outcome": False})
    async def run():
        turn = await conversation.ask(task.goal, document=None, task=task, scope=task.scope,
            model=FunctionModel(stream_function=main), review_model=FunctionModel(stream_function=reviewer), approve_action=approve)
        assert turn.status == "completed", turn.error
        assert [action.status for action in turn.actions] == ["completed", "completed"]
        assert [action.approval_settings.reviewer for action in turn.actions] == ["user", "assistant"]
        assert [action.approval_settings.access.sandbox for action in turn.actions] == ["restricted", "restricted"]
        assert [action.approval_basis for action in turn.actions] == ["user", "assistant"]
        assert len(pending) == 1 and task.requests == 4 and task.review_requests == 1
        assert turn.approval_settings.reviewer == "assistant" and task.approval.reviewer == "user"
        restored = Conversation.open(conversation.path)
        assert restored.approval_settings.mode == "full_access"
        assert restored.turns[-1].approval_settings == turn.approval_settings
        assert (tmp_path / "outside-0.txt").read_text() == "result-0"
        assert (tmp_path / "outside-1.txt").read_text() == "result-1"
    asyncio.run(run())


def test_public_message_changes_only_current_turn_reviewer_and_rejects_old_turn(tmp_path: Path) -> None:
    with connection(tmp_path) as channel:
        channel.send({"action": "configure", "key": "synthetic-settings"})
        assert channel.receive()["event"] == "configured"
        channel.send({"action": "send", "prompt": "等待停止，检查当前选择"})
        started = channel.receive()
        assert started["event"] == "started"
        while (chunk := channel.receive())["event"] != "text":
            pass
        channel.send({"action": "set_turn_reviewer", "task_id": started["task_id"], "reviewer": "assistant"})
        changed = channel.receive()
        assert changed["event"] == "turn_approval" and changed["saved"] is True
        assert changed["approval_settings"]["reviewer"] == "assistant"
        assert changed["approval_settings"]["revision"] == started["task"]["approval"]["revision"]
        channel.send({"action": "snapshot"})
        saved = channel.receive()["conversations"][0]
        assert saved["approval_settings"]["reviewer"] == "user"
        assert saved["turns"][-1]["approval_settings"]["reviewer"] == "assistant"
        channel.send({"action": "stop"})
        while (finished := channel.receive())["event"] != "finished":
            pass
        assert finished["turn"]["status"] == "cancelled"
        channel.send({"action": "set_turn_reviewer", "task_id": started["task_id"], "reviewer": "user"})
        assert channel.receive()["event"] == "error"


def test_existing_child_freezes_settings_and_new_child_inherits_parent_with_independent_counts(tmp_path: Path) -> None:
    folder = tmp_path / "task"
    (folder / ".git").mkdir(parents=True)
    conversation = Conversation(tmp_path / "records" / "session.json")
    task = Task(goal="按次创建四个明确指定的测试小文件", scope=TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"]))
    human = []
    async def approve(action):
        human.append(action)
        if len(human) == 1:
            conversation.set_turn_reviewer(task.id, "assistant")
            conversation.set_approval_mode("full_access")
        return True
    async def model(messages, _info):
        prompt = next(str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart))
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if prompt.startswith("受委派工作："):
            child = "first" if "受委派工作：first" in prompt else "second"
            if len(results) < 2:
                path = f".git/{child}-{len(results)}.txt"
                yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": path, "content": path}), tool_call_id=f"{child}-{len(results)}")}
            else:
                yield "已创建指定测试文件。"
        elif len(results) < 2:
            child = "first" if not results else "second"
            yield {0: DeltaToolCall(name="delegate_work", json_args=json.dumps({"goal": child, "context": "只创建指定的两个测试小文件", "evidence_requirements": "保存实际文件与动作", "expected_files": [f".git/{child}-0.txt", f".git/{child}-1.txt"]}), tool_call_id=f"delegate-{child}")}
        else:
            yield "两项分工已交回。"
    async def reviewer(_messages, _info):
        yield json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general", "reason": "创建任务明确指定的小文件，无覆盖。", "same_denied_outcome": False})
    async def run():
        turn = await conversation.ask(task.goal, document=None, task=task, scope=task.scope, model=FunctionModel(stream_function=model),
            review_model=FunctionModel(stream_function=reviewer), approve_action=approve)
        assert turn.status == "completed", turn.error
        first, second = [Conversation.open(Path(work.record_path)).turns[-1] for work in turn.delegations]
        assert first.status == second.status == "completed"
        assert [action.approval_settings.reviewer for action in first.actions] == ["user", "user"]
        assert [action.approval_settings.reviewer for action in second.actions] == ["assistant", "assistant"]
        assert first.approval_settings.reviewer == "user" and second.approval_settings.reviewer == "assistant"
        assert all(action.approval_settings.access.sandbox == "restricted" for action in [*first.actions, *second.actions])
        assert len(human) == 2 and task.requests == turn.requests == 3 and task.review_requests == 0
        assert task.total_requests == 11
        assert first.requests == 3 and second.requests == 5
        assert first.request_account.review_requests == 0 and second.request_account.review_requests == 2
        assert [work.review_requests for work in turn.delegations] == [0, 2]
        for child in ("first", "second"):
            for index in range(2):
                assert (folder / ".git" / f"{child}-{index}.txt").read_text() == f".git/{child}-{index}.txt"
    asyncio.run(run())
