"""Permission behavior through the public conversation and executor boundaries."""

import asyncio
import json
from pathlib import Path
import pytest

from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from myrk.conversation import Conversation, TaskScope


def test_precise_multiline_edit_preserves_bom_and_crlf_bytes(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    original = b"\xef\xbb\xbffirst\r\nsecond\r\nuntouched\r\n"
    target.write_bytes(original)
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": "notes.txt"}), ("edit_text", {"path": "notes.txt", "old_text": "first\nsecond", "new_text": "updated\nsecond"})]
        if len(results) < len(steps):
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"windows-text-{len(results)}")}
        else:
            yield "原格式已保留。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask("修改两行原文", document=None,
            scope=TaskScope(root=str(tmp_path), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"]), model=FunctionModel(stream_function=model))
        assert turn.status == "completed", turn.error
        assert turn.actions[-1].status == "completed", turn.actions[-1].error
        assert target.read_bytes() == b"\xef\xbb\xbfupdated\r\nsecond\r\nuntouched\r\n"
        assert Path(turn.actions[-1].backup_path).read_bytes() == original
    asyncio.run(run())


@pytest.mark.parametrize("location", ["inside", "outside", "protected"])
def test_create_text_builds_parent_directories_and_refuses_to_overwrite(tmp_path: Path, location: str) -> None:
    folder = tmp_path / "task"
    folder.mkdir()
    target = {"inside": folder / "新资料" / "notes.txt", "outside": tmp_path / "outside" / "新资料" / "notes.txt", "protected": folder / ".agents" / "新资料" / "notes.txt"}[location]
    approvals = []
    async def decide(action):
        approvals.append(action)
        return True
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("create_text", {"path": str(target), "content": "第一版"}),
                 ("create_text", {"path": str(target), "content": "不得覆盖"}),
                 ("read_text", {"path": str(target)})]
        if len(results) < len(steps):
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"create-{len(results)}")}
        else:
            assert "第一版" in str(results[-1].content)
            yield "新建完成，同名原件保留。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask("新建资料", document=None,
            scope=TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"]),
            model=FunctionModel(stream_function=model), approve_action=decide)
        assert turn.status == "completed", turn.error
        assert [action.status for action in turn.actions] == ["completed", "failed", "completed"]
        assert turn.actions[0].sandbox_type == "windowsMxc"
        assert turn.actions[0].before_sha256 is None
        assert turn.actions[0].after_sha256 is not None
        assert len(approvals) == (0 if location == "inside" else 2)
    asyncio.run(run())


@pytest.mark.parametrize("location,approval_count", [("inside", 0), ("outside", 1), ("protected", 1)])
def test_complete_write_and_delete_preserve_exact_originals(tmp_path: Path, location: str, approval_count: int) -> None:
    folder = tmp_path / "task"
    folder.mkdir()
    target = {"inside": folder / "notes.txt", "outside": tmp_path / "outside.txt", "protected": folder / ".agents" / "notes.txt"}[location]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("abc", encoding="utf-8")
    approvals = []
    async def decide(action):
        approvals.append(action)
        return True
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": str(target)}), ("write_text", {"path": str(target), "content": "hello"}), ("delete_file", {"path": str(target)})]
        if len(results) < len(steps):
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"whole-{len(results)}")}
        else:
            yield "完整写入后删除，原件已留存。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask("完整改写及删除", document=None,
            scope=TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"]),
            model=FunctionModel(stream_function=model), approve_action=decide)
        assert turn.status == "completed", turn.error
        assert len(approvals) == approval_count * 2
        assert not target.exists()
        write, delete = turn.actions[-2:]
        assert write.status == delete.status == "completed"
        assert write.before_sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        assert write.after_sha256 == delete.before_sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        assert delete.after_sha256 is None
        assert Path(write.backup_path).read_bytes() == b"abc"
        assert Path(delete.backup_path).read_bytes() == b"hello"
    asyncio.run(run())


def test_generated_command_is_restricted_and_records_real_output(tmp_path: Path) -> None:
    from myrk.executor import POWERSHELL
    folder = tmp_path / "task"
    folder.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("outside-original", encoding="utf-8")
    script = "[Console]::WriteLine('command-output'); [Console]::Error.WriteLine('command-error'); [IO.File]::WriteAllText('" + str(target).replace("'", "''") + "','must-not-write')"
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="run_command", json_args=json.dumps({"program": POWERSHELL, "arguments": ["-NoProfile", "-Command", script]}), tool_call_id="generated-command")}
        else:
            yield "已保留真实命令结果。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask("运行自拟命令", document=None,
            scope=TaskScope(root=str(folder), allowed_actions=["check"], provider="deepseek", recipients=["deepseek"]), model=FunctionModel(stream_function=model))
        assert turn.status == "completed", turn.error
        action = turn.actions[-1]
        assert action.status == "failed" and action.exit_code != 0
        assert action.sandbox_type == "windowsMxc"
        assert "command-output" in action.stdout and "command-error" in action.stderr
        assert target.read_text(encoding="utf-8") == "outside-original"
        assert action.cleanup_complete is True
    asyncio.run(run())


def test_command_preserves_receipts_in_a_long_chat_folder(tmp_path: Path) -> None:
    from myrk.executor import POWERSHELL
    folder = tmp_path / "task"
    folder.mkdir()
    records = tmp_path / ("r" * (175 - len(str(tmp_path)) - 1))
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="run_command", json_args=json.dumps({"program": POWERSHELL, "arguments": ["-NoProfile", "-Command", "[Console]::WriteLine('long-chat-output')"]}), tool_call_id="long-chat-command")}
        else:
            yield "较长聊天位置的真实结果已保留。"
    async def run():
        conversation = Conversation(records / ("s" * 32 + ".json"))
        turn = await conversation.ask("运行正常输出命令", document=None,
            scope=TaskScope(root=str(folder), allowed_actions=["check"], provider="deepseek", recipients=["deepseek"]), model=FunctionModel(stream_function=model))
        action = turn.actions[-1]
        assert action.status == "completed", action.error
        assert action.stdout.strip() == "long-chat-output"
        assert action.cleanup_complete is True and action.sandbox_type == "windowsMxc"
        from myrk.checks import CheckReceipt
        from hashlib import sha256
        receipt=CheckReceipt.model_validate_json(Path(action.receipt_path).read_bytes())
        assert (receipt.action_id,receipt.exit_code,receipt.sandbox_type)==(action.id,0,"windowsMxc")
        assert receipt.argv==[POWERSHELL,"-NoProfile","-Command","[Console]::WriteLine('long-chat-output')"]
        assert receipt.cwd==str(folder)
        assert receipt.stdout_sha256==sha256(Path(action.stdout_path).read_bytes()).hexdigest()
        assert Conversation.open(conversation.path).turns[-1].actions[-1].stdout == action.stdout
    asyncio.run(run())


def test_complete_write_accepts_text_larger_than_the_windows_command_line(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_bytes(b"abc")
    content = "甲" * 7000 + "真实末尾"
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": "notes.txt"}), ("write_text", {"path": "notes.txt", "content": content})]
        if len(results) < len(steps):
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"large-write-{len(results)}")}
        else:
            yield "整份文字已写入。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask("整份写入长资料", document=None,
            scope=TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"]), model=FunctionModel(stream_function=model))
        assert turn.status == "completed", turn.error
        assert turn.actions[-1].status == "completed", turn.actions[-1].error
        assert target.read_text(encoding="utf-8") == content
        assert Path(turn.actions[-1].backup_path).read_bytes() == b"abc"
    asyncio.run(run())


@pytest.mark.parametrize("tool", ["write_text", "delete_file"])
def test_changed_original_is_never_overwritten_or_deleted(tmp_path: Path, tool: str) -> None:
    target = tmp_path / "notes.txt"
    target.write_bytes(b"prepared-original")
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"notes.txt"}', tool_call_id="conflict-read")}
        elif len(results) == 1:
            target.write_bytes(b"other-writers-current-version")
            arguments = {"path": "notes.txt", **({"content": "must-not-write"} if tool == "write_text" else {})}
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(arguments), tool_call_id="conflict-mutation")}
        else:
            yield "已保留其他人的改动。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask("改写或删除已读版本", document=None,
            scope=TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"]), model=FunctionModel(stream_function=model))
        assert turn.status == "completed", turn.error
        assert turn.actions[-1].status == "conflict"
        assert turn.actions[-1].effect == "not_started"
        assert target.read_bytes() == b"other-writers-current-version"
    asyncio.run(run())


def test_exact_command_expansion_preserves_denied_reads_and_does_not_inherit_credentials(tmp_path: Path, monkeypatch) -> None:
    from myrk.conversation import Task
    from myrk.executor import POWERSHELL
    folder = tmp_path / "task"
    folder.mkdir()
    target = tmp_path / "outside.txt"
    target.write_bytes(b"before")
    denied = tmp_path / "denied.txt"
    denied.write_bytes(b"explicit-denied-content-1729")
    monkeypatch.setenv("MYRK_UNAUTHORIZED_CREDENTIAL", "must-not-inherit-credential-8371")
    task = Task(goal="扩大确切写入位置", scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]), request_limit=10)
    task.approval.access.denied_paths = [str(denied)]
    approvals = []
    async def decide(action):
        approvals.append(action)
        return True
    script = "$ErrorActionPreference='Stop'; [IO.File]::WriteAllText('" + str(target).replace("'", "''") + "','approved-write'); [Console]::WriteLine([Environment]::GetEnvironmentVariable('MYRK_UNAUTHORIZED_CREDENTIAL')); [Console]::WriteLine([IO.File]::ReadAllText('" + str(denied).replace("'", "''") + "'))"
    async def model(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="run_command", json_args=json.dumps({"program": POWERSHELL, "arguments": ["-NoProfile", "-Command", script], "additional_write_paths": [str(target)]}), tool_call_id="expanded-command")}
        else:
            yield "确切写入已发生，禁止读取仍被挡住。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None,
            task=task, scope=task.scope, model=FunctionModel(stream_function=model), approve_action=decide)
        assert turn.status == "completed", turn.error
        action = turn.actions[-1]
        assert len(approvals) == 1 and approvals[0].prepared_parameters["additional_write_paths"] == [str(target)]
        assert action.status == "failed" and action.exit_code != 0 and action.cleanup_complete is True
        assert "explicit-denied-content-1729" not in action.stdout
        assert "must-not-inherit-credential-8371" not in action.stdout + action.stderr
        assert target.read_bytes() == b"approved-write"
        assert denied.read_bytes() == b"explicit-denied-content-1729"
    asyncio.run(run())


def test_delete_approval_is_bound_to_the_file_version_shown_before_waiting(tmp_path: Path) -> None:
    target = tmp_path / ".agents" / "notes.txt"
    target.parent.mkdir()
    target.write_bytes(b"prepared-original")
    called = False
    async def decide(action):
        assert action.before_sha256 is not None
        target.write_bytes(b"changed-while-waiting")
        return True
    async def model(messages, _info):
        nonlocal called
        if not called:
            called = True
            yield {0: DeltaToolCall(name="delete_file", json_args='{"path":".agents/notes.txt"}', tool_call_id="version-bound-delete")}
        else:
            yield "等待期间的其他写入已保留。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask("删除明确版本", document=None,
            scope=TaskScope(root=str(tmp_path), allowed_actions=[], provider="deepseek", recipients=["deepseek"]), model=FunctionModel(stream_function=model), approve_action=decide)
        assert turn.status == "completed", turn.error
        assert turn.actions[-1].status == "conflict"
        assert target.read_bytes() == b"changed-while-waiting"
    asyncio.run(run())


def test_declined_delete_reports_its_preparation_read_and_keeps_the_original(tmp_path: Path) -> None:
    target = tmp_path / ".agents" / "notes.txt"
    target.parent.mkdir()
    target.write_bytes(b"abc")
    approvals = []
    async def decide(action):
        approvals.append(action)
        assert action.before_sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        assert action.prepared_parameters["read_sha256"] == action.before_sha256
        return False
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="delete_file", json_args='{"path":".agents/notes.txt"}', tool_call_id="declined-delete")}
        else:
            assert "已读取原件" in str(results[-1].content)
            assert "未读取" not in str(results[-1].content)
            yield "原件已为准备删除而读取；用户拒绝后文件保留。"
    async def run():
        conversation = Conversation(tmp_path / "records" / "session.json")
        turn = await conversation.ask("准备删除一个文件并由本人决定", document=None,
            scope=TaskScope(root=str(tmp_path), allowed_actions=[], provider="deepseek", recipients=["deepseek"]), model=FunctionModel(stream_function=model), approve_action=decide)
        assert len(approvals) == 1
        action = turn.actions[-1]
        assert action.status == "denied" and action.effect == "not_started"
        assert action.backup_path is None
        assert target.read_bytes() == b"abc"
        assert "已读取原件" in action.error
        assert Conversation.open(conversation.path).turns[-1].actions[-1].error == action.error
    asyncio.run(run())


@pytest.mark.parametrize("location,allowed", [("inside", ["read"]), ("inside", ["edit"]), ("outside", ["read", "edit"])])
def test_legacy_delete_does_not_read_before_its_original_scope_allows_it(tmp_path: Path, location: str, allowed: list[str]) -> None:
    from myrk.conversation import Task
    folder = tmp_path / "task"
    folder.mkdir()
    target = folder / "notes.txt" if location == "inside" else tmp_path / "outside.txt"
    target.write_bytes(b"legacy-private-original")
    task = Task(goal="旧记录删除范围", scope=TaskScope(root=str(folder), allowed_actions=allowed, provider="deepseek", recipients=["deepseek"]), request_limit=10)
    task.approval.access.legacy_scope = True
    task.approval.access.denied_paths = [str(target)]
    approvals = []
    async def decide(action):
        approvals.append(action)
        return True
    async def model(messages, _info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="delete_file", json_args=json.dumps({"path": str(target)}), tool_call_id="legacy-delete-without-read")}
        else:
            yield "旧范围没有被扩大。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None,
            task=task, scope=task.scope, model=FunctionModel(stream_function=model), approve_action=decide)
        assert turn.status == "completed", turn.error
        assert not approvals
        assert turn.actions[-1].status == "denied"
        assert turn.actions[-1].before_sha256 is None and turn.actions[-1].backup_path is None
        assert target.read_bytes() == b"legacy-private-original"
    asyncio.run(run())


def test_legacy_outside_delete_uses_a_separately_approved_read_version(tmp_path: Path) -> None:
    from myrk.conversation import Task
    folder = tmp_path / "task"
    folder.mkdir()
    target = tmp_path / "outside.txt"
    target.write_bytes(b"abc")
    task = Task(goal="旧记录先批准读取再删除", scope=TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"]), request_limit=10)
    task.approval.access.legacy_scope = True
    approvals = []
    async def decide(action):
        approvals.append(action)
        return True
    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": str(target)}), ("delete_file", {"path": str(target)})]
        if len(results) < len(steps):
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"legacy-approved-{len(results)}")}
        else:
            yield "两次确切许可均已记录。"
    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None,
            task=task, scope=task.scope, model=FunctionModel(stream_function=model), approve_action=decide)
        assert turn.status == "completed", turn.error
        assert [action.kind for action in approvals] == ["read", "delete"]
        assert approvals[-1].before_sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        assert turn.actions[-1].status == "completed" and not target.exists()
        assert Path(turn.actions[-1].backup_path).read_bytes() == b"abc"
    asyncio.run(run())


def test_request_approval_reads_outside_task_folder_without_asking(tmp_path: Path) -> None:
    folder = tmp_path / "task"
    folder.mkdir()
    source = tmp_path / "outside.txt"
    source.write_text("允许读取的日常资料", encoding="utf-8")
    approvals = []

    async def decide(action):
        approvals.append(action)
        return False

    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_text", json_args=json.dumps({"path": str(source)}), tool_call_id="read-outside")}
        else:
            assert "允许读取的日常资料" in str(results[-1].content)
            yield "已读到实际资料。"

    async def run():
        conversation = Conversation(tmp_path / "records" / "session.json")
        turn = await conversation.ask("读取指定资料", document=None,
            scope=TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"]),
            model=FunctionModel(stream_function=model), approve_action=decide)
        assert turn.status == "completed", turn.error
        assert not approvals
        assert turn.actions[0].status == "completed"
        assert turn.actions[0].sandbox_type == "windowsMxc"
        assert turn.actions[0].approval_settings.reviewer == "user"
    asyncio.run(run())


def test_large_text_read_preserves_the_real_ending_through_executor_messages(tmp_path: Path) -> None:
    source = tmp_path / "large.txt"
    source.write_text("甲" * 45000 + "\n真实结尾7319", encoding="utf-8")

    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_text", json_args=json.dumps({"path": "large.txt", "max_lines": 2}), tool_call_id="large-read")}
        else:
            assert "真实结尾7319" in str(results[-1].content)
            yield "读到真实末尾。"

    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask("读取长资料", document=None,
            scope=TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"]),
            model=FunctionModel(stream_function=model))
        assert turn.status == "completed", turn.error
        assert turn.actions[0].status == "completed", turn.actions[0].error
    asyncio.run(run())


def test_old_record_keeps_its_original_scope_and_is_backed_up_before_upgrade(tmp_path: Path) -> None:
    original = {"version": 4, "messages": [], "turns": [{"prompt": "旧工作", "answer": "", "status": "cancelled",
        "task": {"id": "legacy-task", "goal": "旧工作", "request_limit": 10,
                 "scope": {"root": str(tmp_path), "allowed_actions": ["read"], "provider": "deepseek", "recipients": ["deepseek"]}}}]}
    path = tmp_path / "session.json"
    raw = json.dumps(original, ensure_ascii=False).encode("utf-8")
    path.write_bytes(raw)
    reopened = Conversation.open(path)
    assert reopened.turns[0].task.approval.access.legacy_scope is True
    reopened.save()
    assert next((tmp_path / "backups").glob("session-v4-*.json")).read_bytes() == raw
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 10
    assert Conversation.open(path).turns[0].task.approval.access.legacy_scope is True


def test_new_work_in_an_old_chat_keeps_its_saved_permission_choice(tmp_path: Path) -> None:
    from myrk.conversation import Task
    folder = tmp_path / "task"
    folder.mkdir()
    old_task = Task(goal="旧任务", scope=TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"]), request_policy="shared", request_limit=10)
    old_task.approval.access.legacy_scope = True
    path = tmp_path / "records" / "session.json"
    path.parent.mkdir()
    old_payload = {"version": 5, "messages": [], "turns": [{"prompt": "旧任务", "answer": "", "status": "cancelled",
        "task": old_task.model_dump(mode="json", exclude={"request_policy", "total_requests", "max_turns", "turns_used", "delegation_max_turns"})}]}
    path.write_text(json.dumps(old_payload, ensure_ascii=False), encoding="utf-8")

    async def model(_messages, _info):
        yield "继续原聊天。"

    async def run():
        conversation = Conversation.open(path)
        turn = await conversation.ask("新工作", document=None, scope=old_task.scope, model=FunctionModel(stream_function=model), new_task=True)
        assert turn.task.approval.access.legacy_scope is True
        assert turn.task.approval.revision == old_task.approval.revision
        assert conversation.turns[0].task.request_policy == "shared"
        assert conversation.turns[0].task.request_limit == 10 and conversation.turns[0].task.requests == 0
        assert turn.task.request_policy == "independent" and turn.task.max_turns is None
    asyncio.run(run())


def test_explicitly_denied_directory_cannot_be_listed_by_the_file_tool(tmp_path: Path) -> None:
    from myrk.conversation import Task
    folder = tmp_path / "task"
    denied = tmp_path / "denied"
    folder.mkdir()
    denied.mkdir()
    (denied / "private-name.txt").write_text("测试内容", encoding="utf-8")
    task = Task(goal="查看目录", scope=TaskScope(root=str(folder), allowed_actions=["list"], provider="deepseek", recipients=["deepseek"]), request_limit=10)
    task.approval.access.denied_paths = [str(denied)]

    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="list_files", json_args=json.dumps({"path": str(denied)}), tool_call_id="denied-list")}
        else:
            assert "private-name" not in str(results[-1].content)
            yield "禁止位置未列出。"

    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None,
            task=task, scope=task.scope, model=FunctionModel(stream_function=model))
        assert turn.status == "completed", turn.error
        assert turn.actions[0].status == "failed"
    asyncio.run(run())


def test_changed_authorization_invalidates_a_waiting_approval(tmp_path: Path) -> None:
    from myrk.conversation import Task
    folder = tmp_path / "task"
    folder.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("原件", encoding="utf-8")
    task = Task(goal="改写", scope=TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"]), request_limit=10)

    async def decide(action):
        task.approval.revision = "new-user-requirements"
        return True

    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": str(target)}), ("edit_text", {"path": str(target), "old_text": "原件", "new_text": "不得写入"})]
        if len(results) < 2:
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"stale-{len(results)}")}
        else:
            yield "旧决定未执行。"

    async def run():
        turn = await Conversation(tmp_path / "records" / "session.json").ask(task.goal, document=None,
            task=task, scope=task.scope, model=FunctionModel(stream_function=model), approve_action=decide)
        assert target.read_text(encoding="utf-8") == "原件"
        assert turn.actions[-1].status == "denied"
        assert turn.actions[-1].effect == "not_started"
        assert "失效" in turn.actions[-1].error
    asyncio.run(run())


@pytest.mark.parametrize("location,approval_count", [("inside", 0), ("outside", 1), ("protected", 1)])
def test_precise_edit_uses_system_limits_and_preserves_the_original(tmp_path: Path, location: str, approval_count: int) -> None:
    folder = tmp_path / "task"
    folder.mkdir()
    target = {"inside": folder / "notes.txt", "outside": tmp_path / "outside.txt", "protected": folder / ".agents" / "notes.txt"}[location]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("苹果 3\n梨 2", encoding="utf-8")
    approvals = []

    async def decide(action):
        approvals.append(action)
        return True

    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": str(target)}), ("edit_text", {"path": str(target), "old_text": "苹果 3", "new_text": "苹果 5"})]
        if len(results) < len(steps):
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"edit-{len(results)}")}
        else:
            yield "精确改写完成。"

    async def run():
        conversation = Conversation(tmp_path / "records" / "session.json")
        turn = await conversation.ask("精确改写", document=None,
            scope=TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"]),
            model=FunctionModel(stream_function=model), approve_action=decide)
        assert turn.status == "completed", turn.error
        assert len(approvals) == approval_count
        edit = turn.actions[-1]
        assert edit.status == "completed", edit.error
        assert edit.sandbox_type == "windowsMxc"
        assert target.read_text(encoding="utf-8") == "苹果 5\n梨 2"
        assert Path(edit.backup_path).read_text(encoding="utf-8") == "苹果 3\n梨 2"
        assert Conversation.open(conversation.path).turns[-1].actions[-1].approval_settings == edit.approval_settings
    asyncio.run(run())


def test_precise_edit_of_project_configuration_waits_for_the_user(tmp_path: Path) -> None:
    folder = tmp_path / "task"
    target = folder / ".codex" / "settings.md"
    target.parent.mkdir(parents=True)
    target.write_text("原有设置", encoding="utf-8")
    approvals = []

    async def decide(action):
        approvals.append(action)
        return False

    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        steps = [("read_text", {"path": ".codex/settings.md"}),
                 ("edit_text", {"path": ".codex/settings.md", "old_text": "原有设置", "new_text": "新设置"})]
        if len(results) < len(steps):
            name, arguments = steps[len(results)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"protected-{len(results)}")}
        else:
            yield "已保留原设置。"

    async def run():
        conversation = Conversation(tmp_path / "records" / "session.json")
        turn = await conversation.ask("修改设置", document=None,
            scope=TaskScope(root=str(folder), allowed_actions=["read", "edit"], provider="deepseek", recipients=["deepseek"]),
            model=FunctionModel(stream_function=model), approve_action=decide)
        assert turn.status == "completed", turn.error
        assert len(approvals) == 1
        assert approvals[0].target == str(target.resolve())
        assert approvals[0].approval_settings.reviewer == "user"
        assert target.read_text(encoding="utf-8") == "原有设置"
        assert turn.actions[-1].status == "denied"
    asyncio.run(run())
