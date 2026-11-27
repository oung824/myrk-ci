"""Action-reporting evidence in requests serialized by the real provider SDK."""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path

import httpx2
from openai import AsyncOpenAI
import pytest
from pydantic_ai import models
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from myrk.conversation import Conversation, Task, TaskScope
from myrk.provider import ProviderConfig


def sdk_response(*, tool: str | None = None, arguments: dict | None = None,
                 text: str = "Synthetic transport completed.", call_id: str = "synthetic") -> httpx2.Response:
    delta: dict = {"role": "assistant"}
    if tool is None:
        delta["content"] = text
    else:
        delta["tool_calls"] = [{"index": 0, "id": call_id, "type": "function",
                                "function": {"name": tool, "arguments": json.dumps(arguments or {})}}]
    chunk = {"id": call_id, "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
             "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if tool else "stop"}],
             "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}}
    return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                           text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")


@asynccontextmanager
async def synthetic_deepseek(respond):
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        async with AsyncOpenAI(api_key="synthetic-only", base_url="https://api.deepseek.com",
                               http_client=http, max_retries=0) as client:
            yield OpenAIChatModel("deepseek-flash", provider=DeepSeekProvider(openai_client=client))


def supplied_context(payload: dict) -> str:
    return "\n".join(str(message["content"]) for message in payload["messages"]
                     if message["role"] not in {"assistant", "tool"})


def test_completed_command_retains_unobserved_file_effects_in_next_sdk_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    target = folder / "check_report.py"
    target.write_bytes(b"abc")
    scope = TaskScope(root=str(folder), allowed_actions=["read", "check"],
                      provider="deepseek", recipients=["deepseek"])
    task = Task(goal="计算check_report.py的SHA-256并说明证据范围。", scope=scope,
                provider_config=ProviderConfig(context_window_override=128000))
    requests: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        assert len(requests) <= 2, "The synthetic exchange has one command and one continuation."
        if len(requests) == 1:
            arguments = {
                "program": r"C:\Program Files\PowerShell\7\pwsh.exe",
                "arguments": ["-NoProfile", "-NonInteractive", "-Command",
                              "(Get-FileHash -LiteralPath 'check_report.py' -Algorithm SHA256).Hash"],
            }
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "hash-only",
                "type": "function", "function": {"name": "run_command", "arguments": json.dumps(arguments)}}]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "Synthetic transport completed."}
            finish = "stop"
        chunks = [
            {"id": f"facts-{len(requests)}", "object": "chat.completion.chunk", "created": 1,
             "model": "deepseek-flash", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": f"facts-{len(requests)}", "object": "chat.completion.chunk", "created": 1,
             "model": "deepseek-flash", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
             "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}},
        ]
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n")

    async def run():
        async with synthetic_deepseek(respond) as model:
            return await Conversation(tmp_path / "records" / "conversation.json").ask(
                task.goal, document=None, task=task, scope=scope, model=model,
            )

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    turn = asyncio.run(run())
    assert turn.status == "completed", turn.error
    assert len(turn.actions) == 1
    action = turn.actions[0]
    assert action.kind == "command" and action.delegation_id is None
    assert action.status == action.effect == "completed", action.error
    assert action.exit_code == 0 and action.sandbox_type == "windowsMxc"
    assert action.cleanup_complete is True
    assert target.read_bytes() == b"abc"
    assert len(requests) == 2
    tool_results = [message for message in requests[1]["messages"] if message["role"] == "tool"]
    assert len(tool_results) == 1 and tool_results[0]["tool_call_id"] == "hash-only"
    assert "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD" in tool_results[0]["content"]

    supplied = supplied_context(requests[1])
    action_fact = next((line for line in supplied.splitlines() if action.id in line), "")
    assert action_fact, "The next actual SDK request must identify the recorded command Action."
    for fact in ("主助理", "状态：已完成", "影响记录：已完成", "退出码：0", "内部逐文件影响未完整观测"):
        assert fact in action_fact, (fact, action_fact)
    for guidance in (
        "哈希计算属于读取字节",
        "不等于把正文交给模型或运行脚本",
        "不得仅凭没有显式读取工具调用，就断言完全没读",
        "用户明确禁止程序或命令时",
        "不为补齐报告自行启动命令",
    ):
        assert guidance in supplied, guidance


def test_metadata_and_failed_text_decoding_retain_distinct_read_evidence_in_sdk_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    invalid = folder / "invalid.txt"
    invalid.write_bytes(b"visible-prefix\xff\xfe")
    source = folder / "source.txt"
    source.write_text("independent-source-evidence-9071", encoding="utf-8")
    scope = TaskScope(root=str(folder), allowed_actions=["list", "read"],
                      provider="deepseek", recipients=["deepseek"])
    task = Task(goal="列出资料并分别读取两个文本文件，按实际结果报告。", scope=scope,
                provider_config=ProviderConfig(context_window_override=128000))
    requests: list[dict] = []
    steps = [
        ("metadata-only", "list_files", {"path": "."}),
        ("invalid-encoding", "read_text", {"path": "invalid.txt"}),
        ("source-evidence", "read_text", {"path": "source.txt"}),
    ]

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        assert len(requests) <= 4, "The synthetic exchange has three file actions and one continuation."
        if len(requests) <= len(steps):
            call_id, name, arguments = steps[len(requests) - 1]
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": call_id,
                "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "Synthetic transport completed."}
            finish = "stop"
        chunk = {"id": f"read-facts-{len(requests)}", "object": "chat.completion.chunk", "created": 1,
                 "model": "deepseek-flash", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}}
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")

    async def run():
        async with synthetic_deepseek(respond) as model:
            return await Conversation(tmp_path / "records" / "conversation.json").ask(
                task.goal, document=None, task=task, scope=scope, model=model,
            )

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    turn = asyncio.run(run())
    assert turn.status == "completed", turn.error
    assert len(turn.actions) == 3
    metadata, failed_read, completed_read = turn.actions
    assert [action.tool_call_id for action in turn.actions] == ["metadata-only", "invalid-encoding", "source-evidence"]
    assert [action.kind for action in turn.actions] == ["list", "read", "read"]
    assert metadata.status == metadata.effect == "completed"
    assert failed_read.status == "failed" and failed_read.effect == "unknown"
    assert failed_read.sandbox_type == "windowsMxc"
    assert failed_read.error is not None and "UTF-8" in failed_read.error
    assert completed_read.status == completed_read.effect == "completed"
    assert completed_read.sandbox_type == "windowsMxc"
    assert invalid.read_bytes() == b"visible-prefix\xff\xfe"
    assert source.read_text(encoding="utf-8") == "independent-source-evidence-9071"
    assert len(requests) == 4
    tool_results = {message["tool_call_id"]: message["content"]
                    for message in requests[-1]["messages"] if message["role"] == "tool"}
    assert "invalid.txt" in tool_results["metadata-only"] and "source.txt" in tool_results["metadata-only"]
    assert "independent-source-evidence-9071" not in tool_results["metadata-only"]
    assert "UTF-8" in tool_results["invalid-encoding"]
    assert "[source.txt:1] independent-source-evidence-9071" in tool_results["source-evidence"]

    supplied = supplied_context(requests[-1])
    for action, expected in (
        (metadata, ("主助理", "状态：已完成", "影响记录：已完成", "列举文件名和元信息", "未读取正文")),
        (failed_read, ("主助理", "状态：失败", "影响记录：未知", "读取范围未知", "不能断言未读取")),
        (completed_read, ("主助理", "状态：已完成", "影响记录：已完成", "已取得读取结果")),
    ):
        action_fact = next((line for line in reversed(supplied.splitlines()) if action.id in line), "")
        assert action_fact, ("The next actual SDK request must identify each file Action.", action.id)
        for fact in expected:
            assert fact in action_fact, (fact, action_fact)


def test_delegation_keeps_actor_ownership_and_unique_action_facts_in_actual_sdk_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    (folder / "main.txt").write_text("main-source-4129", encoding="utf-8")
    artifact = folder / "result.txt"
    artifact.write_text("independent-artifact-7319", encoding="utf-8")
    scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="主助理读取资料，委派读取既有产物，再独立核对全文。", scope=scope,
                provider_config=ProviderConfig(context_window_override=128000))
    requests: list[tuple[bool, dict]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        child = any(message["role"] == "user" and isinstance(message.get("content"), str)
                    and message["content"].startswith("受委派工作：") for message in payload["messages"])
        requests.append((child, payload))
        assert len(requests) <= 6, "The synthetic delegation has four main requests and two child requests."
        results = {message["tool_call_id"]: message["content"]
                   for message in payload["messages"] if message["role"] == "tool"}
        if child:
            if "child-source" not in results:
                return sdk_response(tool="read_text", arguments={"path": "result.txt"}, call_id="child-source")
            return sdk_response(text="Original synthetic child report.", call_id="child-answer")
        if "main-source" not in results:
            return sdk_response(tool="read_text", arguments={"path": "main.txt"}, call_id="main-source")
        if "delegation" not in results:
            return sdk_response(tool="delegate_work", arguments={
                "goal": "读取既有result.txt", "context": "只读既有产物，不修改文件。",
                "evidence_requirements": "引用产物原文。", "expected_files": ["result.txt"],
            }, call_id="delegation")
        if "artifact-verification" not in results:
            work = json.loads(results["delegation"])
            return sdk_response(tool="verify_delegation", arguments={"delegation_id": work["id"],
                "path": "result.txt", "expected_text": "independent-artifact-7319"}, call_id="artifact-verification")
        return sdk_response(call_id="main-answer")

    async def run():
        async with synthetic_deepseek(respond) as model:
            return await Conversation(tmp_path / "records" / "delegation.json").ask(
                task.goal, document=None, task=task, scope=scope, model=model,
            )

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    turn = asyncio.run(run())
    assert turn.status == "completed", turn.error
    assert len(turn.delegations) == 1
    work = turn.delegations[0]
    assert work.status == "completed" and work.verification_status == "passed"
    assert work.report == "Original synthetic child report."
    assert artifact.read_text(encoding="utf-8") == "independent-artifact-7319"
    assert len(turn.actions) == len({action.id for action in turn.actions}) == 4
    assert all(action.status == "completed" for action in turn.actions)
    child_action = next(action for action in turn.actions if action.delegation_id == work.id)
    main_action = next(action for action in turn.actions if action.tool_call_id == "main-source")
    verification = next(action for action in turn.actions if action.kind == "verify")
    assert child_action.kind == "read" and child_action.tool_call_id == "child-source"
    assert child_action.sandbox_type == main_action.sandbox_type == verification.sandbox_type == "windowsMxc"
    child = Conversation.open(Path(work.record_path)).turns[-1]
    assert len(child.actions) == 1 and child.actions[0].id == child_action.id
    main_requests = [payload for is_child, payload in requests if not is_child]
    child_requests = [payload for is_child, payload in requests if is_child]
    assert len(main_requests) == 4 and len(child_requests) == 2
    main_facts = turn.execution_facts
    child_facts = child.execution_facts
    assert "动作摘要：本助理3条，其他助理1条" in main_facts
    assert "动作摘要：本助理1条，其他助理0条" in child_facts
    for action in turn.actions:
        assert main_facts.count(action.id) == 1, (action.id, main_facts)
    main_line = next(line for line in main_facts.splitlines() if main_action.id in line)
    child_in_main = next(line for line in main_facts.splitlines() if child_action.id in line)
    child_in_child = next(line for line in child_facts.splitlines() if child_action.id in line)
    assert "主助理（本人）" in main_line
    assert work.id in child_in_main and "（其他助理）" in child_in_main
    assert work.id in child_in_child and "（本人）" in child_in_child
    verification_line = next(line for line in main_facts.splitlines() if verification.id in line)
    assert "已读回声明产物并比较全文，不验证所有过程自述" in verification_line
    main_wire = supplied_context(main_requests[-1])
    child_wire = supplied_context(child_requests[-1])
    assert all(action.id in main_wire for action in turn.actions)
    assert any(child_action.id in line and "状态：已完成" in line and "（其他助理）" in line
               for line in main_wire.splitlines())
    assert any(child_action.id in line and "状态：已完成" in line and "（本人）" in line
               for line in child_wire.splitlines())
    assert any(verification.id in line and "不验证所有过程自述" in line for line in main_wire.splitlines())


def test_reopened_task_sends_current_facts_without_rewriting_saved_messages_or_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    source = folder / "source.txt"
    source.write_text("source-evidence-retained-5683", encoding="utf-8")
    scope = TaskScope(root=str(folder), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="读取指定资料并保留本次记录。", scope=scope,
                provider_config=ProviderConfig(context_window_override=128000))
    path = tmp_path / "records" / "reopen.json"
    original_answer = "原始合成回答：完全没有读取任何文件。"
    requests: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        assert len(requests) <= 3, "Reopening records adds one request and executes no further tool."
        if len(requests) == 1:
            return sdk_response(tool="read_text", arguments={"path": "source.txt"}, call_id="saved-source")
        return sdk_response(text=original_answer if len(requests) == 2 else "Synthetic continuation.",
                            call_id=f"saved-answer-{len(requests)}")

    async def run():
        async with synthetic_deepseek(respond) as model:
            first = await Conversation(path).ask(task.goal, document=None, task=task, scope=scope, model=model)
            assert first.status == "completed", first.error
            before = json.loads(path.read_text(encoding="utf-8"))
            reopened = Conversation.open(path)
            saved_task = reopened.turns[-1].task
            assert saved_task is not None
            second = await reopened.ask("仅据已保存证据继续回答，不再运行命令。", document=None,
                                        task=saved_task, scope=saved_task.scope, model=model)
            return first, second, before, json.loads(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    first, second, before, after = asyncio.run(run())
    assert second.status == "completed", second.error
    assert len(first.actions) == 1 and not second.actions
    action = first.actions[0]
    assert action.kind == "read" and action.status == action.effect == "completed"
    assert source.read_text(encoding="utf-8") == "source-evidence-retained-5683"
    assert first.answer == before["turns"][0]["answer"] == after["turns"][0]["answer"] == original_answer
    assert after["messages"][:len(before["messages"])] == before["messages"]
    assert after["turns"][0]["actions"] == before["turns"][0]["actions"]
    assert Conversation.open(path).turns[0].answer == original_answer
    assert len(requests) == 3
    facts = second.execution_facts
    action_line = next(line for line in facts.splitlines() if action.id in line)
    assert "主助理（本人）" in action_line and "已取得读取结果" in action_line
    assert "哈希计算属于读取字节" in facts
    assert "不为补齐报告自行启动命令" in facts
    wire = supplied_context(requests[-1])
    assert action.id in wire
    assert "哈希计算属于读取字节" in wire and "不为补齐报告自行启动命令" in wire
    assert requests[-1]["messages"][:len(requests[1]["messages"])] == requests[1]["messages"]
    assert any(action.id in line and "状态：已完成" in line and "已取得读取结果" in line
               for line in wire.splitlines())
    sent_history = "\n".join(str(message.get("content", "")) for message in requests[-1]["messages"])
    assert original_answer in sent_history
    assert "[source.txt:1] source-evidence-retained-5683" in sent_history


def test_bounded_action_summary_keeps_preparation_read_when_deletion_is_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "materials"
    folder.mkdir()
    long_target = "./" * 70 + "metadata-suffix"
    (folder / "metadata-suffix").mkdir()
    target = folder / ".agents" / "notes.txt"
    target.parent.mkdir()
    target.write_bytes(b"abc")
    scope = TaskScope(root=str(folder), allowed_actions=["list", "read", "edit"],
                      provider="deepseek", recipients=["deepseek"])
    task = Task(goal="列举指定目录，准备删除受保护资料，由本人决定。", scope=scope,
                provider_config=ProviderConfig(context_window_override=128000))
    requests: list[dict] = []
    approved_action_ids: list[str] = []

    async def decline(action):
        approved_action_ids.append(action.id)
        assert action.kind == "delete" and action.before_sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        return False

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        assert len(requests) <= 11, "The summary is inspected after nine metadata actions and one declined deletion."
        if len(requests) <= 9:
            return sdk_response(tool="list_files", arguments={"path": long_target}, call_id=f"metadata-{len(requests)}")
        if len(requests) == 10:
            return sdk_response(tool="delete_file", arguments={"path": ".agents/notes.txt"}, call_id="declined-deletion")
        return sdk_response(call_id="declined-answer")

    async def run():
        async with synthetic_deepseek(respond) as model:
            return await Conversation(tmp_path / "records" / "bounded.json").ask(
                task.goal, document=None, task=task, scope=scope, model=model, approve_action=decline,
            )

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    turn = asyncio.run(run())
    assert turn.status == "completed", turn.error
    assert len(turn.actions) == 10 and len(requests) == 11
    assert all(action.kind == "list" and action.status == "completed" for action in turn.actions[:-1])
    assert all(action.target == long_target for action in turn.actions[:-1])
    deletion = turn.actions[-1]
    assert approved_action_ids == [deletion.id]
    assert deletion.kind == "delete" and deletion.status == "denied" and deletion.effect == "not_started"
    assert deletion.denial_source == "user"
    assert deletion.before_sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert deletion.backup_path is None and target.read_bytes() == b"abc"
    facts = turn.execution_facts
    lines = [line for line in facts.splitlines() if line.startswith("动作 ")]
    assert len(lines) == 8
    assert "按记录顺序列最后8条，省略2条" in facts
    assert "不是完整文件访问审计" in facts
    assert long_target not in facts and "metadata-suffix" not in facts
    delete_target_is_truncated = len(deletion.target) > 120
    assert sum("目标截短" in line for line in lines) == 7 + delete_target_is_truncated
    assert all(action.id not in facts for action in turn.actions[:2])
    assert all(facts.count(action.id) == 1 for action in turn.actions[2:])
    deletion_line = next(line for line in lines if deletion.id in line)
    assert ("目标截短" in deletion_line) is delete_target_is_truncated
    assert "状态：已拒绝" in deletion_line
    assert "所提议动作未执行，不排除准备读取" in deletion_line
    assert "为版本核对已读取原件" in deletion_line
    assert "影响记录只针对所提议的删除" in deletion_line
    assert "未读取" not in deletion_line
    wire = supplied_context(requests[-1])
    assert "按记录顺序列最后8条，省略2条" in wire
    assert all(action.id in wire for action in turn.actions)
    assert any(deletion.id in line and "状态：已拒绝" in line and "为版本核对已读取原件" in line
               for line in wire.splitlines())
