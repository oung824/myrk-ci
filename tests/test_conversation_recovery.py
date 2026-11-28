import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.function import DeltaToolCall

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
