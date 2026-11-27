import asyncio
import json
import os
import subprocess
import sys
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


def test_resume_keeps_completed_replays_but_edits_another_part_and_checks_new_result(tmp_path: Path) -> None:
    from pydantic_ai.exceptions import ModelHTTPError
    folder = tmp_path / "authorized"
    folder.mkdir()
    notes = folder / "notes.txt"
    notes.write_text("苹果 3\n梨 2", encoding="utf-8")
    script = folder / "check.py"
    script.write_text("from pathlib import Path\ntext=Path('notes.txt').read_text(encoding='utf-8')\nwith Path('runs.txt').open('a',encoding='utf-8') as f: f.write(text.replace('\\n',';')+'\\n')\nassert text in {'苹果 5\\n梨 2','苹果 5\\n梨 4'}\n", encoding="utf-8")
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit", "check"], provider="deepseek", recipients=["deepseek"], checks=[{"name": "真实核对", "argv": [sys.executable, str(script)]}])
    first_edit = ("edit_text", {"path": "notes.txt", "old_text": "苹果 3", "new_text": "苹果 5"})
    first_steps = [("read_text", {"path": "notes.txt"}), first_edit, ("run_check", {"name": "真实核对"})]
    resume_steps = [first_edit, ("run_check", {"name": "真实核对"}), ("read_text", {"path": "notes.txt"}),
                    ("edit_text", {"path": "notes.txt", "old_text": "梨 2", "new_text": "梨 4"}), ("run_check", {"name": "真实核对"})]

    async def source(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) == 3:
            raise ModelHTTPError(503, "controlled-source")
        name, args = first_steps[len(results)]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"first-{len(results)}")}

    async def continued(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        position = len(results) - 3
        if position == len(resume_steps):
            yield "两处均已改写，并逐次检查真实文件。"
            return
        if position == 3:
            assert "苹果 5" in str(results[-1].content) and "梨 2" in str(results[-1].content)
        name, args = resume_steps[position]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"resume-{position}")}

    async def run():
        conversation = Conversation(tmp_path / "records" / "record.json")
        first = await conversation.ask("先改苹果，核对后再改梨并核对", document=None, scope=scope, request_limit=20, model=FunctionModel(stream_function=source))
        assert first.status == "failed" and first.service_failure
        original = first.model_dump(mode="json")
        reopened = Conversation.open(conversation.path)
        resumed = await reopened.resume_task(first.task.id, model=FunctionModel(stream_function=continued))
        assert resumed.status == "completed", resumed.error
        assert notes.read_text(encoding="utf-8") == "苹果 5\n梨 4"
        assert (folder / "runs.txt").read_text(encoding="utf-8").splitlines() == ["苹果 5;梨 2", "苹果 5;梨 4"]
        assert [action.kind for action in resumed.actions] == ["read", "edit", "check"]
        assert resumed.task.id == original["task"]["id"] and resumed.task.scope == scope
        assert resumed.task.requests == original["task"]["requests"] + 6
        assert resumed.task.active_seconds >= original["task"]["active_seconds"]
        assert reopened.turns[0].status == "failed" and reopened.turns[0].error == original["error"]
        assert [action.model_dump(mode="json") for action in reopened.turns[0].actions] == original["actions"]
        assert Conversation.open(conversation.path).turns[-1].checks_passed is True
    asyncio.run(run())


def test_resume_does_not_repeat_a_completed_edit_when_replacement_keeps_the_old_fragment(tmp_path: Path) -> None:
    from pydantic_ai.exceptions import ModelHTTPError
    target = tmp_path / "notes.txt"
    target.write_text("甲", encoding="utf-8")
    scope = TaskScope(root=str(tmp_path), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"])
    steps = [("read_text", {"path": "notes.txt"}), ("edit_text", {"path": "notes.txt", "old_text": "甲", "new_text": "甲乙"})]
    async def source(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) == 2:
            raise ModelHTTPError(503, "controlled-retained-fragment")
        name, args = steps[len(results)]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"retained-before-{len(results)}")}
    async def continued(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) == 4:
            yield "保留已有甲乙，不重复追加乙。"
            return
        if len(results) == 3:
            assert "[notes.txt:1] 甲乙" in str(results[-1].content)
        name, args = steps[len(results) - 2]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"retained-after-{len(results)}")}
    async def run():
        conversation = Conversation(tmp_path / "records" / "record.json")
        first = await conversation.ask("把甲改成甲乙，只做一次", document=None, scope=scope, model=FunctionModel(stream_function=source))
        assert first.service_failure and target.read_text(encoding="utf-8") == "甲乙"
        original = first.model_dump(mode="json")
        reopened = Conversation.open(conversation.path)
        resumed = await reopened.resume_task(first.task.id, model=FunctionModel(stream_function=continued))
        assert resumed.status == "completed", resumed.error
        assert target.read_text(encoding="utf-8") == "甲乙"
        assert [action.kind for action in resumed.actions] == ["read"]
        assert resumed.task.id == original["task"]["id"] and resumed.task.scope == scope
        assert resumed.task.requests == original["task"]["requests"] + 3
        assert resumed.task.active_seconds >= original["task"]["active_seconds"]
        assert [action.model_dump(mode="json") for action in reopened.turns[0].actions] == original["actions"]
        assert Path(first.actions[-1].backup_path).read_text(encoding="utf-8") == "甲"
    asyncio.run(run())


def test_resume_can_repeat_edit_input_after_reading_a_new_current_file_version(tmp_path: Path) -> None:
    from pydantic_ai.exceptions import ModelHTTPError
    target = tmp_path / "notes.txt"
    target.write_text("value 3", encoding="utf-8")
    script = tmp_path / "check.py"
    script.write_text("from pathlib import Path\np=Path('runs.txt')\ntext=Path('notes.txt').read_text()\nassert text=='value 5'\nfirst=not p.exists()\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nif first: Path('notes.txt').write_text('value 3')\n", encoding="utf-8")
    scope = TaskScope(root=str(tmp_path), allowed_actions=["read", "edit", "check"], provider="deepseek", recipients=["deepseek"], checks=[{"name": "核对并首次恢复原文", "argv": [sys.executable, str(script)]}])
    steps = [("read_text", {"path": "notes.txt"}), ("edit_text", {"path": "notes.txt", "old_text": "value 3", "new_text": "value 5"}), ("run_check", {"name": "核对并首次恢复原文"})]
    async def source(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) == 3:
            raise ModelHTTPError(503, "controlled-current-version")
        name, args = steps[len(results)]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"current-before-{len(results)}")}
    async def continued(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) == 6:
            yield "读回当前文件后重新办理所需修改和检查。"
            return
        name, args = steps[len(results) - 3]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"current-after-{len(results)}")}
    async def run():
        conversation = Conversation(tmp_path / "records" / "record.json")
        first = await conversation.ask("检查会首次恢复原文，之后再改一次", document=None, scope=scope, request_limit=20, model=FunctionModel(stream_function=source))
        assert first.service_failure and target.read_text() == "value 3"
        resumed = await Conversation.open(conversation.path).resume_task(first.task.id, model=FunctionModel(stream_function=continued))
        assert resumed.status == "completed", resumed.error
        assert target.read_text() == "value 5" and (tmp_path / "runs.txt").read_text() == "xx"
        assert [action.kind for action in resumed.actions] == ["read", "edit", "check"]
    asyncio.run(run())


def test_approved_provider_switch_keeps_completed_files_checks_and_task_budget(tmp_path: Path) -> None:
    from myrk.provider import ProviderConfig
    from pydantic_ai.exceptions import ModelHTTPError
    folder = tmp_path / "authorized"
    folder.mkdir()
    notes = folder / "notes.txt"
    notes.write_text("苹果 3", encoding="utf-8")
    script = folder / "check.py"
    script.write_text("from pathlib import Path\np=Path('runs.txt')\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nassert Path('notes.txt').read_text(encoding='utf-8')=='苹果 5'\n", encoding="utf-8")
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit", "check"], provider="deepseek", recipients=["deepseek"], checks=[{"name": "真实核对", "argv": [sys.executable, str(script)]}])
    async def source(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": "notes.txt"}), ("edit_text", {"path": "notes.txt", "old_text": "苹果 3", "new_text": "苹果 5"}), ("run_check", {"name": "真实核对"})]
        if len(results) == 3:
            raise ModelHTTPError(503, "controlled-source")
        name, args = steps[len(results)]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"source-{len(results)}")}
    async def target(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) == 3:
            yield {0: DeltaToolCall(name="run_check", json_args='{"name":"真实核对"}', tool_call_id="duplicate-check")}
        else:
            yield "改写和检查已经完成，保留未做事项。"
    async def run():
        conversation = Conversation(tmp_path / "record.json")
        first = await conversation.ask("改写后检查", document=None, scope=scope, model=FunctionModel(stream_function=source))
        assert first.status == "failed" and first.service_failure
        assert first.task.requests == 6
        config = ProviderConfig(name="DeepSeek", base_url="http://127.0.0.1:9001/v1", model="second", protocol="openai-chat")
        change = conversation.preview_provider_change(first.task.id, config)
        assert change.compatible and "苹果 5" in change.materials and "runs.txt" not in change.materials
        task = conversation.decide_provider_change(change.id, True)
        assert task.provider_config == config
        resumed = await conversation.resume_task(task.id, model=FunctionModel(stream_function=target))
        assert resumed.status == "completed", resumed.error
        assert resumed.task.id == first.task.id and resumed.task.scope == scope and resumed.task.requests == 8
        assert notes.read_text(encoding="utf-8") == "苹果 5"
        assert (folder / "runs.txt").read_text() == "x"
    asyncio.run(run())


@pytest.mark.parametrize("extra", ["thinking", "signed_tool", "orphan"])
def test_provider_switch_refuses_unreliable_history_without_dropping_original_fields(tmp_path: Path, extra: str) -> None:
    from myrk.provider import ProviderConfig
    from myrk.conversation import Task, Turn, ConversationRecord
    from pydantic_ai.messages import ModelRequest, ModelResponse, ThinkingPart, TextPart, ToolCallPart
    task = Task(goal="继续原工作", scope=None, request_limit=10, requests=2)
    parts = [ThinkingPart("必须保留的额外思考", provider_name="deepseek")] if extra == "thinking" else [ToolCallPart("read_text", {}, "call-1", id="signed-id" if extra == "signed_tool" else None)]
    messages = [ModelRequest(parts=[]), ModelResponse(parts=parts)]
    if extra == "signed_tool":
        messages.append(ModelRequest(parts=[ToolReturnPart("read_text", "真实原文", "call-1")]))
    path = tmp_path / "history.json"
    old_record = ConversationRecord(version=4, messages=messages, turns=[Turn(prompt=task.goal, answer="部分回答", status="failed", service_failure=True, task=task)]).model_dump(mode="json")
    for field in ("request_policy", "total_requests", "delegation_max_turns", "max_turns", "turns_used", "approval"):
        old_record["turns"][0]["task"].pop(field, None)
    path.write_text(json.dumps(old_record, ensure_ascii=False), encoding="utf-8")
    conversation = Conversation.open(path)
    original_messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
    change = conversation.preview_provider_change(task.id, ProviderConfig(name="普通服务", base_url="http://127.0.0.1:9001/v1", model="second", protocol="openai-chat"))
    assert not change.compatible
    with pytest.raises(ValueError):
        conversation.decide_provider_change(change.id, True)
    assert json.loads(path.read_text(encoding="utf-8"))["messages"] == original_messages
    assert conversation.turns[-1].task.requests == 2


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


@pytest.mark.parametrize("requested", ["../outside/outside.txt", "alias/outside.txt"])
def test_request_approval_reads_outside_and_junction_targets_with_system_limits(tmp_path: Path, requested: str) -> None:
    folder = tmp_path / "materials"
    outside = tmp_path / "outside"
    folder.mkdir()
    outside.mkdir()
    private = outside / "outside.txt"
    private.write_text("outside-readable-fixture", encoding="utf-8")
    if requested.startswith("alias"):
        subprocess.run([
            "C:/Program Files/PowerShell/7/pwsh.exe", "-NoProfile", "-Command",
            "New-Item -ItemType Junction -Path $env:MYRK_TEST_LINK -Target $env:MYRK_TEST_TARGET | Out-Null",
        ], check=True, env={**os.environ, "MYRK_TEST_LINK": str(folder / "alias"), "MYRK_TEST_TARGET": str(outside)})
    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_text", json_args=json.dumps({"path": requested}), tool_call_id="outside-read")}
        else:
            yield str(results[-1].content)

    turn = asyncio.run(Conversation(tmp_path / "record.json").ask(
        "读取指定资料", document=None, model=FunctionModel(stream_function=respond),
        scope=TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"]),
    ))
    assert "outside-readable-fixture" in turn.answer
    assert turn.actions[0].status == "completed"
    assert turn.actions[0].sandbox_type == "windowsMxc"


def test_folder_search_does_not_enter_a_junction_outside_the_authorized_folder(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    outside = tmp_path / "outside"
    folder.mkdir()
    outside.mkdir()
    (folder / "rule.txt").write_text("获准搜索的规定", encoding="utf-8")
    (outside / "outside.txt").write_text("外部规定不得搜索", encoding="utf-8")
    alias = folder / "alias"
    subprocess.run([
        "C:/Program Files/PowerShell/7/pwsh.exe", "-NoProfile", "-Command",
        "New-Item -ItemType Junction -Path $env:MYRK_TEST_LINK -Target $env:MYRK_TEST_TARGET | Out-Null",
    ], check=True, env={**os.environ, "MYRK_TEST_LINK": str(alias), "MYRK_TEST_TARGET": str(outside)})
    entered: list[str] = []

    def observe_directory(event: str, args: tuple[object, ...]) -> None:
        if event == "os.scandir" and str(args[0]) in {str(alias), str(outside)}:
            entered.append(str(args[0]))

    sys.addaudithook(observe_directory)

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="search_text", json_args='{"query":"规定"}', tool_call_id="search-folder")}
        else:
            yield str(results[-1].content)

    turn = asyncio.run(Conversation(tmp_path / "record.json").ask(
        "搜索规定", document=None, model=FunctionModel(stream_function=respond),
        scope=TaskScope(root=str(folder), allowed_actions=["search"], provider="deepseek", recipients=["deepseek"]),
    ))
    assert "[rule.txt:1] 获准搜索的规定" in turn.answer
    assert entered == []


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


def test_long_search_result_marks_omission_and_keeps_the_full_sourced_output(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "rule.txt").write_text("\n".join(f"needle {number}: " + "详细规定" * 30 for number in range(1, 601)), encoding="utf-8")

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="search_text", json_args='{"query":"needle"}', tool_call_id="long-search")}
        else:
            yield str(results[-1].content)

    turn = asyncio.run(Conversation(tmp_path / "records" / "conversation.json").ask(
        "搜索规定", document=None, model=FunctionModel(stream_function=respond),
        scope=TaskScope(root=str(folder), allowed_actions=["search"], provider="deepseek", recipients=["deepseek"]),
    ))
    assert "其余结果已省略；全文位置：" in turn.answer
    assert turn.actions[0].full_output_path is not None
    full_output = Path(turn.actions[0].full_output_path).read_text(encoding="utf-8")
    assert "[rule.txt:600] needle 600:" in full_output
    assert len(full_output) > len(turn.answer)
    assert "已省略；全文位置：" in turn.actions[0].output
    saved = json.loads((tmp_path / "records" / "conversation.json").read_text(encoding="utf-8"))
    raw = [part["content"] for message in saved["messages"] for part in message["parts"] if part["part_kind"] == "tool-return"]
    assert raw == [full_output]
    assert "全文证据编号：" in turn.answer and "[rule.txt:1] needle 1:" in turn.answer
    assert "[rule.txt:300] needle 300:" not in turn.answer


@pytest.mark.parametrize("allowed,contents,status,explanation", [
    ([], "获准范围内但未允许读取的内容".encode(), "denied", "未允许"),
    (["read"], "旧格式资料".encode("gbk"), "failed", "UTF-8"),
])
def test_read_permission_and_real_text_errors_are_visible_as_action_outcomes(tmp_path: Path, allowed: list, contents: bytes, status: str, explanation: str) -> None:
    from myrk.conversation import Task
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "rule.txt").write_bytes(contents)

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"rule.txt"}', tool_call_id="read-error")}
        else:
            yield str(results[-1].content)

    path = tmp_path / "records" / "conversation.json"
    scope = TaskScope(root=str(folder), allowed_actions=allowed, provider="deepseek", recipients=["deepseek"])
    task = Task(goal="读取旧授权规定", scope=scope, request_limit=10)
    task.approval.access.legacy_scope = True
    turn = asyncio.run(Conversation(path).ask(
        "读取规定", document=None, model=FunctionModel(stream_function=respond),
        scope=scope, task=task,
    ))
    assert turn.actions[0].status == status
    assert explanation in turn.answer
    assert Conversation.open(path).turns[-1].actions[0].error == turn.actions[0].error

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


def test_old_task_deadline_does_not_stop_service_but_explicit_user_stop_does(tmp_path: Path, monkeypatch) -> None:
    from myrk.conversation import Task
    from myrk.task_control import TaskBudget
    now = 100.0
    monkeypatch.setattr("myrk.task_control.monotonic", lambda: now)
    task = Task(goal="超过旧时间字段后继续等待本人停止", scope=None, active_seconds_limit=0.08)
    budget = TaskBudget(task)
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        nonlocal now
        with budget.waiting():
            now += 10
            await asyncio.sleep(0)
        assert task.active_seconds == 0
        yield "用户等待已结束"
        now += .2
        budget.account()
        assert task.active_seconds == pytest.approx(.2)
        assert not budget.token.cancelled
        yield "超过旧时间仍在继续"
        await asyncio.Event().wait()
        yield "停止后不应到达"
    def progress(update):
        if update.kind == "text" and "超过旧时间仍在继续" in update.text:
            budget.token.cancel()
    path = tmp_path / "time.json"
    async def run():
        return await asyncio.wait_for(Conversation(path).ask(task.goal, document=None,
            model=FunctionModel(stream_function=stream), task=task, budget=budget, on_update=progress), timeout=10)
    turn = asyncio.run(run())
    assert turn.status == "cancelled" and turn.error is None
    assert turn.answer == "用户等待已结束超过旧时间仍在继续"
    assert "停止后不应到达" not in turn.answer
    assert task.active_seconds == pytest.approx(.2)
    reopened = Conversation.open(path).turns[-1]
    assert reopened.status == "cancelled" and reopened.answer == turn.answer
    assert reopened.task.requests == reopened.task.total_requests == 1

def test_a_not_started_second_action_waits_for_a_new_supplement_to_be_adopted(tmp_path: Path) -> None:
    from myrk.conversation import Supplement, Task
    from myrk.task_control import TaskBudget
    for name in ["first.txt", "obsolete.txt", "revised.txt"]:
        (tmp_path / name).write_text(name, encoding="utf-8")
    scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="读取两份", scope=scope, request_limit=5)
    budget = TaskBudget(task)
    async def stream(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"first.txt"}', tool_call_id="first"),
                   1: DeltaToolCall(name="read_text", json_args='{"path":"obsolete.txt"}', tool_call_id="obsolete")}
        elif not any("revised.txt:1" in str(part.content) for part in results):
            assert "改为读取revised.txt" in str(messages)
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"revised.txt"}', tool_call_id="revised")}
        else:
            yield "按补充完成"
    def update(event):
        if event.kind == "action" and event.action.status == "started" and not task.supplements:
            task.supplements.append(Supplement(text="改为读取revised.txt"))
    turn = asyncio.run(Conversation(tmp_path / "two-actions.json").ask("读取两份", document=None, scope=scope, task=task, budget=budget, model=FunctionModel(stream_function=stream), on_update=update))
    assert turn.status == "completed"
    assert [action.target for action in turn.actions] == ["first.txt", "revised.txt"]
    assert task.supplements[0].applied_request == 2


def test_authorized_precise_edit_uses_real_read_and_preserves_original(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    target = folder / "notes.txt"
    target.write_text("苹果 3\n梨子 2\n", encoding="utf-8")
    async def stream(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": "notes.txt"}), ("edit_text", {"path": "notes.txt", "old_text": "苹果 3", "new_text": "苹果 5"})]
        if len(results) < len(steps):
            name, args = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"edit-{len(results)}")}
        else:
            yield str(results[-1].content)
    path = tmp_path / "records" / "edit.json"
    turn = asyncio.run(Conversation(path).ask("把苹果数量改成5", document=None, scope=TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"]), model=FunctionModel(stream_function=stream)))
    assert turn.status == "completed"
    assert target.read_text(encoding="utf-8") == "苹果 5\n梨子 2\n"
    assert [action.kind for action in turn.actions] == ["read", "edit"]
    assert turn.actions[-1].backup_path is not None
    assert Path(turn.actions[-1].backup_path).read_text(encoding="utf-8") == "苹果 3\n梨子 2\n"
    assert Conversation.open(path).turns[-1].actions[-1].status == "completed"


def test_sensitive_actions_wait_for_individual_decisions_and_denials_have_no_effect(tmp_path: Path) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    target = folder / ".agents" / "notes.txt"
    target.parent.mkdir()
    target.write_text("keep-me", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private-outside", encoding="utf-8")
    opened = []
    def audit(event, args):
        if event == "open" and str(args[0]) == str(outside):
            opened.append(str(args[0]))
    sys.addaudithook(audit)
    pending = []
    async def approve(action):
        pending.append(action)
        assert target.exists() and not opened
        assert action.status == "awaiting_approval" and action.approval_id
        assert action.target in action.impact
        return False
    async def stream(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": ".agents/notes.txt"}), ("delete_file", {"path": ".agents/notes.txt"}), ("read_text", {"path": "../outside.txt"}), ("send_text", {"path": ".agents/notes.txt", "url": "http://127.0.0.1:1/never-send"})]
        if len(results) < len(steps):
            name, args = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"sensitive-{len(results)}")}
        else:
            yield "\n".join(str(part.content) for part in results)
    turn = asyncio.run(Conversation(tmp_path / "records" / "denied.json").ask("检查敏感动作拒绝", document=None, scope=TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"]), model=FunctionModel(stream_function=stream), approve_action=approve))
    assert len(pending) == 2
    assert len({action.approval_id for action in pending}) == 2
    assert [action.status for action in turn.actions] == ["completed", "denied", "completed", "denied"]
    assert target.read_text(encoding="utf-8") == "keep-me" and opened == []


def test_text_table_calculation_uses_real_decimal_values_and_source_rows(tmp_path: Path) -> None:
    (tmp_path / "amounts.csv").write_text("item,amount\napple,0.10\npear,0.20\nrefund,-0.05\n", encoding="utf-8")
    async def stream(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="describe_extension", json_args='{"name":"calculate_table"}', tool_call_id="describe-sum")}
        elif len(results) == 1:
            definition = results[-1].content
            yield {0: DeltaToolCall(name="call_extension", json_args=json.dumps({"name": "calculate_table", "schema_version": definition["schema_version"], "arguments": {"path": "amounts.csv", "column": "amount"}}), tool_call_id="sum")}
        else:
            yield str(results[-1].content)
    turn = asyncio.run(Conversation(tmp_path / "records" / "sum.json").ask("合计金额", document=None, scope=TaskScope(root=str(tmp_path), allowed_actions=["read", "calculate"], provider="deepseek", recipients=["deepseek"]), model=FunctionModel(stream_function=stream)))
    assert "0.25" in turn.answer and "3行" in turn.answer
    assert "[amounts.csv:2]" in turn.answer and "-0.05" in turn.answer
    assert turn.actions[0].kind == "calculate" and turn.actions[0].status == "completed"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_user_specified_check_runs_real_program_and_records_both_streams(tmp_path: Path, exit_code: int) -> None:
    script = tmp_path / "check.py"
    script.write_text(f"import pathlib,sys\nprint(pathlib.Path.cwd())\nprint('actual-stdout')\nprint('actual-stderr', file=sys.stderr)\nsys.exit({exit_code})\n", encoding="utf-8")
    async def stream(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="run_check", json_args='{"name":"检查材料"}', tool_call_id="real-check")}
        else:
            yield "本次处理结束"
    turn = asyncio.run(Conversation(tmp_path / "records" / "check.json").ask("运行指定检查", document=None, scope=TaskScope(root=str(tmp_path), allowed_actions=["check"], provider="deepseek", recipients=["deepseek"], checks=[{"name": "检查材料", "argv": [sys.executable, str(script)]}]), model=FunctionModel(stream_function=stream)))
    action = turn.actions[0]
    assert action.exit_code == exit_code and action.cwd == str(tmp_path)
    assert "actual-stdout" in action.stdout and "actual-stderr" in action.stderr
    assert str(tmp_path) in action.stdout and action.process_id
    assert action.status == ("completed" if exit_code == 0 else "failed")
    assert turn.checks_passed is (exit_code == 0)
    assert turn.delivery_status == "awaiting_acceptance" and turn.status == "completed"


@pytest.mark.parametrize("stop", [False, True])
def test_check_timeout_and_stop_finish_owned_children_but_leave_other_programs(tmp_path: Path, stop: bool) -> None:
    import ctypes
    from pydantic_ai import CancellationToken
    pidfile = tmp_path / "child.pid"
    (tmp_path / "notes.txt").write_text("before-stop", encoding="utf-8")
    script = tmp_path / "long_check.py"
    script.write_text("import pathlib,subprocess,sys,time\nchild=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\npathlib.Path('child.pid').write_text(str(child.pid))\nprint('started-real-child',flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    token = CancellationToken()
    async def stream(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results and stop:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id="read-before-stop")}
        elif not results or stop and len(results) == 1:
            yield {0: DeltaToolCall(name="run_check", json_args='{"name":"持续检查"}', tool_call_id="long-check")}
            if stop:
                yield {1: DeltaToolCall(name="edit_text", json_args='{"path":"notes.txt","old_text":"before-stop","new_text":"must-not-write"}', tool_call_id="edit-after-stop")}
        else:
            yield "实际检查未通过，已有输出保留。"
    async def run():
        async def stopper():
            while not pidfile.exists():
                await asyncio.sleep(0.01)
            token.cancel()
        stopping = asyncio.create_task(stopper()) if stop else None
        try:
            return await Conversation(tmp_path / "records" / "long.json").ask("运行持续检查", document=None, scope=TaskScope(root=str(tmp_path), allowed_actions=["read", "edit", "check"], provider="deepseek", recipients=["deepseek"], checks=[{"name":"持续检查", "argv":[sys.executable,str(script)], "timeout_seconds": 0.3}]), model=FunctionModel(stream_function=stream), cancellation_token=token)
        finally:
            if stopping:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
    try:
        turn = asyncio.run(run())
        action = next(action for action in turn.actions if action.kind == "check")
        assert pidfile.exists() and "started-real-child" in action.stdout
        assert action.status == ("cancelled" if stop else "failed")
        assert action.timed_out is (not stop)
        assert turn.status == ("cancelled" if stop else "completed")
        fact = next(line for line in turn.execution_facts.splitlines() if action.id in line)
        assert f"状态：{'已停止' if stop else '失败'}" in fact
        assert "影响记录：未知" in fact and "内部逐文件影响未完整观测" in fact
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong], ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, int(pidfile.read_text()))
        if handle:
            code = ctypes.c_ulong()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            assert code.value != 259
        assert unrelated.poll() is None
        assert next(action for action in Conversation.open(tmp_path / "records" / "long.json").turns[-1].actions if action.kind == "check").exit_code is not None
        assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "before-stop"
        assert not any(action.kind == "edit" for action in turn.actions)
        assert action.cleanup_complete is True
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_two_tasks_in_one_folder_queue_writes_and_reconsider_changed_original(tmp_path: Path) -> None:
    from myrk.conversation import Task
    target = tmp_path / "notes.txt"
    target.write_text("苹果 3\n", encoding="utf-8")
    script = tmp_path / "external_check.py"
    script.write_text("import pathlib,time\npathlib.Path('check.started').touch()\nwhile not pathlib.Path('read.finished').exists(): time.sleep(.01)\npathlib.Path('notes.txt').write_text('苹果 3\\n外部新增\\n',encoding='utf-8')\n", encoding="utf-8")
    observations = []
    phase = 0
    async def check_stream(messages, info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="run_check", json_args='{"name":"改写检查"}', tool_call_id="folder-check")}
        else:
            yield "检查结束"
    async def edit_stream(messages, info):
        nonlocal phase
        phase += 1
        if phase == 1:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id="folder-read")}
        elif phase in [2, 3]:
            if phase == 2:
                assert "苹果 3" in str(messages)
                (tmp_path / "read.finished").touch()
            if phase == 3:
                assert "原文已变化" in str(messages) and "外部新增" in str(messages)
            yield {0: DeltaToolCall(name="edit_text", json_args='{"path":"notes.txt","old_text":"苹果 3","new_text":"苹果 5"}', tool_call_id=f"folder-edit-{phase}")}
        else:
            yield "重新核对后完成"
    async def run():
        check_scope = TaskScope(root=str(tmp_path), allowed_actions=["check"], provider="deepseek", recipients=["deepseek"], checks=[{"name":"改写检查", "argv":[sys.executable,str(script)]}])
        first = asyncio.create_task(Conversation(tmp_path / "records" / "one.json").ask("检查", document=None, scope=check_scope, model=FunctionModel(stream_function=check_stream), on_update=lambda update: observations.append(("check",update.action.status)) if update.kind == "action" else None))
        while not (tmp_path / "check.started").exists():
            await asyncio.sleep(0.01)
        second = await Conversation(tmp_path / "records" / "two.json").ask("改数量", document=None, scope=TaskScope(root=str(tmp_path),allowed_actions=["read","edit"],provider="deepseek",recipients=["deepseek"]),model=FunctionModel(stream_function=edit_stream),on_update=lambda update: observations.append((update.action.kind,update.action.status)) if update.kind == "action" else None)
        return await first, second
    first, second = asyncio.run(run())
    assert first.checks_passed is True and second.status == "completed"
    assert target.read_text(encoding="utf-8") == "苹果 5\n外部新增\n"
    assert observations.index(("check","completed")) < observations.index(("edit","started"))
    assert [action.status for action in second.actions] == ["completed","conflict","completed"]


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


@pytest.mark.parametrize("kind", ["send", "delete"])
def test_resume_distinguishes_old_sensitive_replay_from_new_content_needing_approval(tmp_path: Path, kind: str) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pydantic_ai.exceptions import ModelHTTPError
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204)
            self.end_headers()
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    target = tmp_path / ".agents" / "notes.txt" if kind == "delete" else tmp_path / "notes.txt"
    target.parent.mkdir(exist_ok=True)
    target.write_text("original-content", encoding="utf-8")
    (tmp_path / "another.txt").write_text("new-content", encoding="utf-8")
    args = {"path": "notes.txt", "url": f"http://127.0.0.1:{server.server_port}/receive"} if kind == "send" else {"path": ".agents/notes.txt"}
    original_steps = [("read_text", {"path": str(target)}), ("send_text" if kind == "send" else "delete_file", args)]
    resumed_steps = original_steps + [("read_text", {"path": "another.txt"}), ("send_text", {**args, "path": "another.txt"})] if kind == "send" else [("delete_file", args), ("delete_file", args)]
    decisions = []
    async def approve(action):
        decisions.append(action)
        return len(decisions) == 1
    async def source(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) == len(original_steps):
            raise ModelHTTPError(503, "controlled-sensitive-source")
        name, parameters = original_steps[len(results)]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(parameters), tool_call_id=f"original-sensitive-{len(results)}")}
    async def continued(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        position = len(results) - len(original_steps)
        if position == len(resumed_steps):
            yield "新敏感动作仍须逐次批准，拒绝后没有影响。"
            return
        if kind == "delete" and position == 1:
            target.write_text("new-current-object", encoding="utf-8")
        name, parameters = resumed_steps[position]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(parameters), tool_call_id=f"resumed-sensitive-{position}")}
    async def run():
        conversation = Conversation(tmp_path / "records" / "sensitive.json")
        first = await conversation.ask("逐次批准敏感动作", document=None, request_limit=20, scope=TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"]), approve_action=approve, model=FunctionModel(stream_function=source))
        assert first.service_failure and len(decisions) == 1
        resumed = await Conversation.open(conversation.path).resume_task(first.task.id, approve_action=approve, model=FunctionModel(stream_function=continued))
        assert resumed.status == "completed", resumed.error
        assert len(decisions) == 2 and decisions[0].id != decisions[1].id
        assert decisions[0].approval_id != decisions[1].approval_id
        assert resumed.actions[-1].status == "denied"
        if kind == "send":
            assert received == [b"original-content"] and target.read_text() == "original-content"
            assert "another.txt" in decisions[1].impact and "new-content" in decisions[1].impact
        else:
            assert target.read_text() == "new-current-object"
            assert Path(first.actions[-1].backup_path).read_bytes() == b"original-content"
    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_actual_receiver_gets_only_the_individually_approved_send_and_delete_keeps_original(tmp_path: Path) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204)
            self.end_headers()
        def log_message(self, format, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = tmp_path / "notes.txt"
    target.write_text("only-approved-material", encoding="utf-8")
    decisions = []
    async def approve(action):
        decisions.append(action)
        if len(decisions) == 2:
            assert received == [] and target.exists()
        return len(decisions) != 1
    async def stream(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path":"notes.txt"}), ("send_text", {"path":"notes.txt","url":f"http://127.0.0.1:{server.server_port}/receive"}), ("send_text", {"path":"notes.txt","url":f"http://127.0.0.1:{server.server_port}/receive"}), ("delete_file", {"path":"notes.txt"})]
        if len(results) < len(steps):
            name,args = steps[len(results)]
            yield {0: DeltaToolCall(name=name,json_args=json.dumps(args),tool_call_id=f"receiver-{len(results)}")}
        else:
            yield "按逐项选择执行"
    try:
        turn = asyncio.run(Conversation(tmp_path / "records" / "receiver.json").ask("逐项确认",document=None,scope=TaskScope(root=str(tmp_path),allowed_actions=["read"],provider="deepseek",recipients=["deepseek"]),approve_action=approve,model=FunctionModel(stream_function=stream)))
        assert received == [b"only-approved-material"] and not target.exists()
        assert [action.status for action in turn.actions] == ["completed","denied","completed","completed"]
        assert Path(turn.actions[-1].backup_path).read_bytes() == b"only-approved-material"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_default_check_deadline_really_ends_the_owned_program_after_sixty_seconds(tmp_path: Path) -> None:
    from time import monotonic
    script = tmp_path / "default_timeout.py"
    script.write_text("import time\nprint('default-60-second-start',flush=True)\ntime.sleep(120)\n",encoding="utf-8")
    async def stream(messages, info):
        if not any(isinstance(part,ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="run_check",json_args='{"name":"默认时限"}',tool_call_id="default-timeout")}
        else:
            yield "检查超时，不视为通过。"
    started = monotonic()
    turn = asyncio.run(Conversation(tmp_path / "records" / "default-timeout.json").ask("运行默认时限检查",document=None,scope=TaskScope(root=str(tmp_path),allowed_actions=["check"],provider="deepseek",recipients=["deepseek"],checks=[{"name":"默认时限","argv":[sys.executable,str(script)]}]),model=FunctionModel(stream_function=stream)))
    elapsed = monotonic() - started
    assert 59 <= elapsed < 75
    assert turn.actions[0].timed_out and turn.actions[0].exit_code is not None
    assert "default-60-second-start" in turn.actions[0].stdout and turn.checks_passed is False
