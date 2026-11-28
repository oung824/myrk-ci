import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.function import DeltaToolCall, DeltaToolCalls

from myrk.conversation import Conversation, TaskScope


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
