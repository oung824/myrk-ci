import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.function import DeltaToolCall, DeltaToolCalls
from pydantic_ai.models.test import TestModel
from pydantic_ai import CancellationToken
from pydantic import ValidationError

from myrk.conversation import Conversation, TaskScope


def test_periodic_progress_does_not_overlap_request_record_save(tmp_path: Path, monkeypatch) -> None:
    directory = tmp_path / "records"
    project = tmp_path / "project"
    project.mkdir()
    delayed = False
    write = Path.write_text
    def slow_pending_write(self, data, *args, **kwargs):
        nonlocal delayed
        result = write(self, data, *args, **kwargs)
        if self.parent == directory and self.suffix == ".tmp" and threading.current_thread() is not threading.main_thread() and not delayed:
            delayed = True
            time.sleep(0.45)
        return result
    monkeypatch.setattr(Path, "write_text", slow_pending_write)
    async def reply(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        yield "请求与定时进度都可靠保存。"
    conversation = Conversation(directory / "record.json")
    scope = TaskScope(root=str(project), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    turn = asyncio.run(conversation.ask("可靠保存请求进展", document=None, scope=scope, model=FunctionModel(stream_function=reply)))
    assert turn.status == "completed", turn.error
    assert conversation.save_failures == ()
    assert Conversation.open(conversation.path).turns[-1].answer == "请求与定时进度都可靠保存。"


def test_answer_uses_the_user_selected_text(tmp_path: Path) -> None:
    document = tmp_path / "采购规则.md"
    document.write_text("采购金额800以上需审批。", encoding="utf-8")
    conversation = Conversation(tmp_path / "conversation.json")

    turn = asyncio.run(
        conversation.ask(
            "读取这份规则。",
            document=document,
            model=TestModel(call_tools=["read_authorized_text"]),
        )
    )

    assert "采购金额800以上需审批。" in turn.answer


def test_reopened_conversation_keeps_previous_turns_and_can_continue(tmp_path: Path) -> None:
    path = tmp_path / "conversation.json"
    conversation = Conversation(path)
    asyncio.run(
        conversation.ask(
            "先记住这条规则。",
            document=None,
            model=TestModel(call_tools=[], custom_output_text="规则已经记录。"),
        )
    )
    reopened = Conversation.open(path)
    asyncio.run(
        reopened.ask(
            "继续解释。",
            document=None,
            model=TestModel(call_tools=[], custom_output_text="这是后续说明。"),
        )
    )

    assert [(turn.prompt, turn.answer) for turn in Conversation.open(path).turns] == [
        ("先记住这条规则。", "规则已经记录。"),
        ("继续解释。", "这是后续说明。"),
    ]


def test_answer_is_visible_before_generation_finishes(tmp_path: Path) -> None:
    chunks: list[str] = []

    async def stream(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str]:
        yield "第一段"
        await asyncio.sleep(0.01)
        assert "".join(chunks) == "第一段"
        yield "第二段"

    turn = asyncio.run(
        Conversation(tmp_path / "conversation.json").ask(
            "逐段说明。",
            document=None,
            model=FunctionModel(stream_function=stream),
            on_update=lambda event: chunks.append(event.text)
            if event.kind == "text"
            else None,
        )
    )

    assert turn.answer == "第一段第二段"


def test_stop_finishes_as_cancelled_and_preserves_partial_answer(tmp_path: Path) -> None:
    token = CancellationToken()

    async def stream(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str]:
        yield "已生成的内容"
        await asyncio.sleep(30)
        yield "不应继续生成"

    conversation = Conversation(tmp_path / "conversation.json")
    turn = asyncio.run(
        conversation.ask(
            "请说明。",
            document=None,
            model=FunctionModel(stream_function=stream),
            cancellation_token=token,
            on_update=lambda event: token.cancel() if event.kind == "text" else None,
        )
    )

    assert (turn.status, Conversation.open(conversation.path).turns[-1].answer) == (
        "cancelled",
        "已生成的内容",
    )


def test_failed_request_keeps_partial_output_without_persisting_error_secrets(
    tmp_path: Path,
) -> None:
    async def stream(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str]:
        yield "已接收的片段"
        raise RuntimeError("private-key-must-not-be-saved")

    path = tmp_path / "conversation.json"
    turn = asyncio.run(
        Conversation(path).ask(
            "请说明。", document=None, model=FunctionModel(stream_function=stream)
        )
    )

    assert (turn.status, turn.answer) == ("failed", "已接收的片段")
    assert "private-key-must-not-be-saved" not in path.read_text(encoding="utf-8")


def test_legacy_request_limit_stops_before_another_request(tmp_path: Path) -> None:
    from myrk.conversation import Task

    document = tmp_path / "规则.txt"
    document.write_text("800以上需审批。", encoding="utf-8")
    turn = asyncio.run(
        Conversation(tmp_path / "conversation.json").ask(
            "读取并说明。",
            document=document,
            model=TestModel(call_tools=["read_authorized_text"]),
            task=Task(goal="旧工作读取并说明", scope=None, request_policy="shared", request_limit=1),
        )
    )

    assert (turn.status, turn.requests) == ("limited", 1)


def test_non_text_secret_file_is_rejected_before_model_processing(tmp_path: Path) -> None:
    document = tmp_path / ".env"
    document.write_text("API_KEY=synthetic-test-secret", encoding="utf-8")
    with pytest.raises(ValueError, match="普通文本"):
        asyncio.run(
            Conversation(tmp_path / "conversation.json").ask(
                "读取资料。", document=document, model=TestModel()
            )
        )


def test_cancelling_before_start_keeps_prior_tool_history(tmp_path: Path) -> None:
    document = tmp_path / "规则.txt"
    document.write_text("800以上需审批。", encoding="utf-8")
    path = tmp_path / "conversation.json"
    conversation = Conversation(path)
    asyncio.run(conversation.ask("读取。", document=document, model=TestModel(call_tools=["read_authorized_text"])))
    token = CancellationToken()
    token.cancel()
    asyncio.run(
        conversation.ask(
            "取消这次。", document=None, model=TestModel(), cancellation_token=token
        )
    )

    async def read_history(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str]:
        has_rule = any(
            isinstance(part, ToolReturnPart) and "800以上需审批。" in str(part.content)
            for message in messages
            for part in message.parts
        )
        yield "已保留原来的规则" if has_rule else "原来的规则丢失"

    turn = asyncio.run(
        Conversation.open(path).ask(
            "继续原任务。",
            document=None,
            model=FunctionModel(stream_function=read_history),
        )
    )

    assert turn.answer == "已保留原来的规则"


def test_failed_record_write_does_not_leave_a_completed_turn(tmp_path: Path) -> None:
    sent = []

    async def source(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        sent.append(messages)
        yield "已经生成的回答。"

    blocked_directory = tmp_path / "not-a-directory"
    blocked_directory.write_text("existing file", encoding="utf-8")
    conversation = Conversation(blocked_directory / "conversation.json")
    with pytest.raises(OSError):
        asyncio.run(
            conversation.ask(
                "请说明。",
                document=None,
                model=FunctionModel(stream_function=source),
            )
        )

    assert (conversation.turns[-1].status, conversation.turns[-1].answer) == (
        "failed",
        "",
    )
    assert not sent
    assert conversation.turns[-1].task.requests == 0
    assert not conversation.turns[-1].request_attempts


@pytest.mark.parametrize(
    "original",
    [
        '{"version":1,"turns":[{"prompt":"原目标","answer":"旧回答","status":"completed"}]}',
        '{"version":1,"messages":[],"turns":[{"prompt":"原目标","answer":"旧回答","status":"completed"}]}',
    ],
)
def test_record_missing_full_history_is_rejected_without_overwrite(
    tmp_path: Path, original: str
) -> None:
    path = tmp_path / "incomplete.json"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValidationError):
        Conversation.open(path)

    assert path.read_text(encoding="utf-8") == original


def test_unreadable_text_keeps_question_and_explains_required_encoding(tmp_path: Path) -> None:
    document = tmp_path / "旧格式.txt"
    document.write_bytes("采购规则需要审批。".encode("gbk"))
    path = tmp_path / "conversation.json"
    turn = asyncio.run(
        Conversation(path).ask("这份规则怎么规定？", document=document, model=TestModel(call_tools=["read_authorized_text"]))
    )

    assert turn.status == "failed"
    assert turn.error is not None and "UTF-8" in turn.error
    assert Conversation.open(path).turns[-1].prompt == "这份规则怎么规定？"


def test_adding_task_authorization_keeps_a_readable_original_record_backup(tmp_path: Path) -> None:
    path = tmp_path / "conversation.json"
    conversation = Conversation(path)
    asyncio.run(conversation.ask("旧问题", document=None, model=TestModel(call_tools=[], custom_output_text="旧答复")))
    old_record = json.loads(path.read_text(encoding="utf-8"))
    old_record["version"] = 1
    old_record.pop("prepared_contexts", None)
    for old_turn in old_record["turns"]:
        old_turn.pop("task", None)
    path.write_text(json.dumps(old_record, ensure_ascii=False), encoding="utf-8")
    original = path.read_bytes()
    conversation = Conversation.open(path)
    asyncio.run(conversation.ask(
        "带任务范围的新问题", document=None, model=TestModel(call_tools=[], custom_output_text="按范围处理"),
        scope=TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"]),
    ))
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 10
    backups = list((tmp_path / "backups").glob("conversation-v1-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert Conversation.open(backups[0]).turns[-1].answer == "旧答复"
    assert Conversation.open(path).turns[-1].task.scope.root == str(tmp_path)


def test_legacy_request_allowance_survives_separate_runs_and_refuses_before_request(tmp_path: Path) -> None:
    from myrk.conversation import Task
    from myrk.task_control import TaskBudget
    task = Task(goal="旧工作共享额度", scope=None, request_policy="shared", request_limit=1)
    budget = TaskBudget(task)
    conversation = Conversation(tmp_path / "budget.json")
    first = asyncio.run(conversation.ask("首次", document=None, model=TestModel(call_tools=[], custom_output_text="已回答"), task=task, budget=budget))
    second = asyncio.run(conversation.ask("第二次", document=None, model=TestModel(call_tools=[], custom_output_text="不该发送"), task=task, budget=budget))
    assert first.answer == "已回答"
    assert second.status == "limited" and second.answer == ""
    assert task.requests == 1
    reopened = Conversation.open(conversation.path)
    assert reopened.turns[-1].task.requests == reopened.turns[-1].task.total_requests == 1
    assert reopened.turns[-1].task.request_policy == "shared"
    prepared = json.loads(conversation.path.read_text(encoding="utf-8"))["prepared_contexts"]
    assert [item["request_attempt"] for item in prepared] == [1, None]


def test_supplement_stays_selected_without_applied_request_if_presend_save_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from myrk.conversation import Supplement, Task

    supplement = Supplement(text="保留这条已接收的补充。")
    task = Task(goal="核对原件。", scope=None, supplements=[supplement])
    conversation = Conversation(tmp_path / "record.json")
    original_save = conversation._save
    rejected_save = False
    sent = False

    def fail_once(now: str) -> None:
        nonlocal rejected_save
        if supplement.applied_request is not None and not rejected_save:
            rejected_save = True
            raise OSError("simulated failure saving the prepared request")
        original_save(now)

    async def respond(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        nonlocal sent
        sent = True
        yield "该请求必须在保存失败时停止。"

    monkeypatch.setattr(conversation, "_save", fail_once)
    turn = asyncio.run(conversation.ask(task.goal, document=None, task=task, model=FunctionModel(stream_function=respond)))
    assert rejected_save and not sent
    assert turn.status == "failed"
    saved_task = Conversation.open(conversation.path).turns[-1].task
    assert saved_task is not None
    saved_supplement, = saved_task.supplements
    assert saved_supplement.id == supplement.id and saved_supplement.selected
    assert saved_supplement.applied_request is None


def test_supplement_reaches_next_request_without_changing_the_started_action_basis(tmp_path: Path) -> None:
    from myrk.conversation import Supplement, Task
    from myrk.task_control import TaskBudget
    (tmp_path / "first.txt").write_text("原始资料", encoding="utf-8")
    (tmp_path / "second.txt").write_text("补充资料", encoding="utf-8")
    scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="开始读取", scope=scope, request_limit=5)
    budget = TaskBudget(task)
    observations = []
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"first.txt"}', tool_call_id="first")}
        elif len(results) == 1:
            assert "请再读取second.txt" in str(messages)
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"second.txt"}', tool_call_id="second")}
        else:
            yield "两份真实资料已读"
    def observe(update):
        observations.append(update)
        if update.kind == "action" and update.action.status == "started" and not task.supplements:
            task.supplements.append(Supplement(text="请再读取second.txt"))
    turn = asyncio.run(Conversation(tmp_path / "supplement.json").ask("开始读取", document=None, scope=scope, task=task, budget=budget, model=FunctionModel(stream_function=stream), on_update=observe))
    assert turn.status == "completed"
    assert task.supplements[0].applied_request == 2
    assert [action.prepared_request for action in turn.actions] == [1, 2]
    assert [action.output for action in turn.actions] == ["[first.txt:1] 原始资料", "[second.txt:1] 补充资料"]


def test_approval_wait_pauses_active_time_and_new_supplement_prevents_old_action(tmp_path: Path, monkeypatch) -> None:
    from myrk.conversation import Supplement, Task
    from myrk.task_control import TaskBudget
    target = tmp_path / ".agents" / "notes.txt"
    target.parent.mkdir()
    target.write_text("keep-me", encoding="utf-8")
    scope = TaskScope(root=str(tmp_path),allowed_actions=["read"],provider="deepseek",recipients=["deepseek"])
    task = Task(goal="删除",scope=scope,request_limit=4,active_seconds_limit=.1)
    budget = TaskBudget(task)
    now = 100.0
    monkeypatch.setattr("myrk.task_control.monotonic", lambda: now)
    async def approve(action):
        nonlocal now
        now += 20
        await asyncio.sleep(.15)
        task.supplements.append(Supplement(text="不要删除，请保留文件"))
        return True
    called = False
    async def stream(messages, info):
        nonlocal called
        if not called:
            called = True
            yield {0: DeltaToolCall(name="delete_file",json_args='{"path":".agents/notes.txt"}',tool_call_id="waiting-delete")}
        else:
            assert "不要删除" in str(messages)
            yield "按补充保留文件"
    turn = asyncio.run(Conversation(tmp_path / "records" / "wait.json").ask("删除",document=None,scope=scope,task=task,budget=budget,approve_action=approve,model=FunctionModel(stream_function=stream)))
    assert turn.status == "completed" and target.read_text(encoding="utf-8") == "keep-me"
    assert turn.actions[0].status == "conflict" and task.supplements[0].applied_request == 2
    assert task.active_seconds < .1
