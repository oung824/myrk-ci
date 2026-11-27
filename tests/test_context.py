import asyncio
import json
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic_ai import CancellationToken

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from myrk.conversation import Conversation, Task, TaskScope
from myrk.provider import ProviderConfig


def test_long_material_can_be_read_back_without_overwriting_original_history(tmp_path: Path) -> None:
    document = tmp_path / "长资料.txt"
    original = "开头原句。\n" + "普通资料。\n" * 2500 + "中间验收原句：金额937需复核。\n" + "其他资料。\n" * 2500 + "结尾原句。"
    document.write_text(original, encoding="utf-8")
    requests: list[list[ModelMessage]] = []

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(messages)
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_authorized_text", json_args="{}", tool_call_id="long-material")}
        elif len(results) == 1:
            preview = str(results[0].content)
            assert "省略" in preview and "开头原句。" in preview and "结尾原句。" in preview
            assert "中间验收原句" not in preview
            evidence_id = re.search(r"全文证据编号：([a-f0-9]{64})", preview).group(1)
            yield {0: DeltaToolCall(name="read_context_evidence", json_args=json.dumps({
                "evidence_id": evidence_id, "start": original.index("中间验收原句"), "length": 40,
            }), tool_call_id="read-middle")}
        else:
            assert "中间验收原句：金额937需复核。" in str(results[-1].content)
            yield "已从全文核对：金额937需复核。"

    path = tmp_path / "records" / "conversation.json"
    turn = asyncio.run(Conversation(path).ask("核对中间规定，尚待用户验收。", document=document, model=FunctionModel(stream_function=respond)))
    assert (turn.status, turn.answer, turn.requests) == ("completed", "已从全文核对：金额937需复核。", 3)
    assert document.read_text(encoding="utf-8") == original
    saved = json.loads(path.read_text(encoding="utf-8"))
    raw = [part for message in saved["messages"] for part in message["parts"] if part["part_kind"] == "tool-return"]
    assert raw[0]["content"] == original
    prepared = saved["prepared_contexts"]
    assert len(prepared) == len(requests) == 3
    assert "中间验收原句" not in str(prepared[1]["messages"])
    assert "中间验收原句" in str(prepared[2]["messages"])
    assert str(document) in "".join(prepared[1]["instructions"])
    assert Conversation.open(path).turns[-1].context_notes


def test_indivisible_user_requirements_stop_before_request_and_keep_original_words(tmp_path: Path) -> None:
    prompt = "必须保留的重要原话，失败未查明，待办不可删。" * 8000
    received: list[list[ModelMessage]] = []

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        received.append(messages)
        yield "不应发送超出本次资料容量的要求。"

    path = tmp_path / "conversation.json"
    turn = asyncio.run(Conversation(path).ask(prompt, document=None, model=FunctionModel(stream_function=respond)))
    assert turn.status == "limited" and turn.requests == 0 and received == []
    assert "资料容量" in turn.error and "原话" in turn.error and "未完成" in turn.error
    assert Conversation.open(path).turns[-1].prompt == prompt


def test_reopened_work_keeps_goal_authorization_failure_pending_work_and_original_words(tmp_path: Path) -> None:
    path = tmp_path / "conversation.json"
    scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])

    async def first(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"missing.txt"}', tool_call_id="failed-read")}
        else:
            yield "实际失败：原资料未找到。待办：找回原文件后核对，不能宣布完成。"

    conversation = Conversation(path)
    original_words = "目标：核对原始证据。重要原话：即使文件找不到也不编造。"
    asyncio.run(conversation.ask(original_words, document=None, scope=scope, model=FunctionModel(stream_function=first)))

    async def resumed(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        visible = str(messages) + str(info.instructions)
        assert original_words in visible
        assert "待办：找回原文件后核对" in visible
        assert json.dumps(str(tmp_path), ensure_ascii=False) in str(info.instructions)
        assert '"allowed_actions": ["read"]' in str(info.instructions)
        assert '"recipients": ["deepseek"]' in str(info.instructions)
        assert '"status": "failed"' in str(info.instructions) and "missing.txt" in str(info.instructions)
        assert {"list_files", "read_text", "search_text", "edit_text", "run_check", "send_text"}.isdisjoint(tool.name for tool in info.function_tools)
        yield "已按原始依据继续，当前没有获得新的文件动作允许。"

    turn = asyncio.run(Conversation.open(path).ask("继续核对。", document=None, model=FunctionModel(stream_function=resumed)))
    assert turn.status == "completed" and "原始依据" in turn.answer


def test_omitted_material_is_searchable_and_forged_evidence_cannot_read_another_file(tmp_path: Path) -> None:
    document = tmp_path / "authorized.txt"
    document.write_text("开头\n" + "普通原文\n" * 3000 + "指定待办：补查发票937。\n" + "其他原文\n" * 3000, encoding="utf-8")
    private = tmp_path / "not-authorized.txt"
    private.write_text("must-not-open-private-material", encoding="utf-8")
    evidence_id = ""

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        nonlocal evidence_id
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_authorized_text", json_args="{}", tool_call_id="selected")}
        elif len(results) == 1:
            evidence_id = re.search(r"全文证据编号：([a-f0-9]{64})", str(results[0].content)).group(1)
            yield {0: DeltaToolCall(name="read_context_evidence", json_args=json.dumps({"evidence_id": evidence_id, "query": "指定待办", "length": 30}), tool_call_id="search-evidence")}
        elif len(results) == 2:
            assert "指定待办：补查发票937。" in str(results[-1].content)
            yield {0: DeltaToolCall(name="read_context_evidence", json_args=json.dumps({"evidence_id": str(private)}), tool_call_id="forged-evidence")}
        else:
            assert "已拒绝" in str(results[-1].content)
            yield "已取回指定待办，未知证据编号被拒绝。"

    opened: list[str] = []

    def observe_open(event: str, args: tuple[object, ...]) -> None:
        if event == "open" and str(args[0]) == str(private):
            opened.append(str(args[0]))

    sys.addaudithook(observe_open)
    turn = asyncio.run(Conversation(tmp_path / "record.json").ask("回查待办。", document=document, model=FunctionModel(stream_function=respond)))
    assert (turn.status, opened) == ("completed", [])


@pytest.mark.parametrize("version", [1, 2, 3])
def test_upgrading_old_history_keeps_a_readable_byte_exact_backup(tmp_path: Path, version: int) -> None:
    path = tmp_path / "conversation.json"
    asyncio.run(Conversation(path).ask("旧版原话", document=None, model=FunctionModel(stream_function=_short_reply)))
    old_record = json.loads(path.read_text(encoding="utf-8"))
    old_record["version"] = version
    for field in ("recovery_checks", "action_evidence", "save_failures", "last_saved_at"):
        old_record.pop(field, None)
    old_record.pop("prepared_contexts", None)
    for turn in old_record["turns"]:
        turn.pop("context_notes", None)
        for field in ("request_owner_id", "request_account", "execution_scope", "execution_facts"):
            turn.pop(field, None)
        for field in ("request_policy", "total_requests", "delegation_max_turns", "max_turns", "turns_used", "approval"):
            turn["task"].pop(field, None)
        turn["task"]["request_limit"] = 10
        turn["task"]["active_seconds_limit"] = 600
    original = json.dumps(old_record, ensure_ascii=False).encode("utf-8")
    path.write_bytes(original)
    asyncio.run(Conversation.open(path).ask("新版继续", document=None, model=FunctionModel(stream_function=_short_reply)))
    backups = list((tmp_path / "backups").glob(f"conversation-v{version}-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert Conversation.open(backups[0]).turns[-1].prompt == "旧版原话"
    upgraded = json.loads(path.read_text(encoding="utf-8"))
    assert upgraded["version"] == 10
    assert upgraded["turns"][0]["task"]["request_policy"] == "shared"
    assert upgraded["turns"][0]["task"]["requests"] == upgraded["turns"][0]["task"]["total_requests"] == 1


async def _short_reply(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
    yield "可读回的答复"


@pytest.mark.parametrize("failure", ["save", "changed"])
def test_unreliable_full_evidence_stops_with_a_visible_reason_and_original_history(tmp_path: Path, failure: str) -> None:
    document = tmp_path / "source.txt"
    original = "原始资料不得丢失。\n" * 3000
    document.write_text(original, encoding="utf-8")
    path = tmp_path / "record.json"
    if failure == "save":
        (tmp_path / "record-context").write_text("阻止专用证据目录创建", encoding="utf-8")

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_authorized_text", json_args="{}", tool_call_id="original-read")}
        else:
            preview = str(results[0].content)
            evidence = Path(re.search(r"全文位置：(.+?)。", preview).group(1))
            evidence.write_text("错误资料，不可继续冒充原文", encoding="utf-8")
            evidence_id = re.search(r"全文证据编号：([a-f0-9]{64})", preview).group(1)
            yield {0: DeltaToolCall(name="read_context_evidence", json_args=json.dumps({"evidence_id": evidence_id}), tool_call_id="changed-read")}

    turn = asyncio.run(Conversation(path).ask("检查全文。", document=document, model=FunctionModel(stream_function=respond)))
    assert turn.status == "failed" and "全文证据" in turn.error and "停止" in turn.error
    assert turn.requests == (1 if failure == "save" else 2)
    assert original in [part["content"] for message in json.loads(path.read_text(encoding="utf-8"))["messages"] for part in message["parts"] if part["part_kind"] == "tool-return"]


def test_long_chat_reduces_accumulated_outputs_before_the_next_request(tmp_path: Path) -> None:
    document = tmp_path / "material.txt"
    original = "真实来源开头。" + "中间资料" * 2200 + "真实来源结尾。"
    document.write_text(original, encoding="utf-8")
    path = tmp_path / "record.json"
    conversation = Conversation(path)
    for number in range(6):
        async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
            if any(isinstance(part, ToolReturnPart) for part in messages[-1].parts):
                yield "已读本次片段；待办：继续核对未完事项。"
            else:
                yield {0: DeltaToolCall(name="read_authorized_text", json_args="{}", tool_call_id=f"accumulated-{number}")}

        turn = asyncio.run(conversation.ask(
            f"第{number + 1}次读取，必须保留最早要求和未完事项。", document=document,
            model=FunctionModel(stream_function=respond, profile={"context_window": 32000}),
            task=Task(goal="连续读取时保留原文证据", scope=None, provider_config=ProviderConfig(auto_compaction=False)),
        ))
        assert turn.status == "completed"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["prepared_contexts"][-1]["omitted"]
    assert all(item["accepted"] for item in saved["prepared_contexts"])
    raw_results = [part["content"] for message in saved["messages"] for part in message["parts"] if part["part_kind"] == "tool-return"]
    assert raw_results == [original] * 6


def test_library_request_normalization_does_not_rewrite_the_original_history(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    original_messages = json.loads(ModelMessagesTypeAdapter.dump_json([
        ModelRequest(parts=[UserPromptPart("第一条重要原话")]),
        ModelRequest(parts=[UserPromptPart("紧接着补充：尚未验收")]),
        ModelResponse(parts=[TextPart("已有部分成果，待检查。")]),
    ]))
    path.write_text(json.dumps({"version": 2, "messages": original_messages, "turns": [{
        "prompt": "第一条重要原话", "answer": "已有部分成果，待检查。", "status": "completed",
    }]}, ensure_ascii=False), encoding="utf-8")
    turn = asyncio.run(Conversation.open(path).ask("继续核查。", document=None, model=FunctionModel(stream_function=_short_reply)))
    assert turn.status == "completed"
    assert json.loads(path.read_text(encoding="utf-8"))["messages"][:3] == original_messages


def test_long_material_can_continue_after_stop_and_reopening_without_losing_evidence(tmp_path: Path) -> None:
    document = tmp_path / "source.txt"
    original = "开头" + "原始资料" * 5000 + "未完成原话：继续核对937。"
    document.write_text(original, encoding="utf-8")
    token = CancellationToken()

    async def partial(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield {0: DeltaToolCall(name="read_authorized_text", json_args="{}", tool_call_id="before-stop")}
        else:
            yield "保留的部分答复。"
            await asyncio.sleep(30)

    path = tmp_path / "record.json"
    stopped = asyncio.run(Conversation(path).ask(
        "读取并核对。", document=document, model=FunctionModel(stream_function=partial), cancellation_token=token,
        on_update=lambda event: token.cancel() if event.kind == "text" else None,
    ))
    before = json.loads(path.read_text(encoding="utf-8"))["messages"]
    assert (stopped.status, stopped.answer) == ("cancelled", "保留的部分答复。")

    async def resumed(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(results) == 1:
            assert "保留的部分答复。" in str(messages)
            evidence_id = re.search(r"全文证据编号：([a-f0-9]{64})", str(results[0].content)).group(1)
            yield {0: DeltaToolCall(name="read_context_evidence", json_args=json.dumps({"evidence_id": evidence_id, "query": "未完成原话"}), tool_call_id="after-stop")}
        else:
            assert "继续核对937。" in str(results[-1].content)
            yield "已从原证据接着核对。"

    continued = asyncio.run(Conversation.open(path).ask("由用户选择继续。", document=None, model=FunctionModel(stream_function=resumed)))
    assert continued.status == "completed"
    assert json.loads(path.read_text(encoding="utf-8"))["messages"][:len(before)] == before


@pytest.mark.parametrize("limit", [1, 2])
def test_long_material_preparation_does_not_add_hidden_model_requests(tmp_path: Path, limit: int) -> None:
    document = tmp_path / "source.txt"
    original = "获准长原文" * 4000
    document.write_text(original, encoding="utf-8")
    received: list[list[ModelMessage]] = []

    async def repeat(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        received.append(messages)
        yield {0: DeltaToolCall(name="read_authorized_text", json_args="{}", tool_call_id=f"limited-{len(received)}")}

    path = tmp_path / "record.json"
    turn = asyncio.run(Conversation(path).ask("读取长资料直到上限。", document=document, model=FunctionModel(stream_function=repeat), request_limit=limit))
    assert (turn.status, turn.requests, len(received)) == ("limited", limit, limit)
    assert original in [part["content"] for message in json.loads(path.read_text(encoding="utf-8"))["messages"] for part in message["parts"] if part["part_kind"] == "tool-return"]


def test_long_context_is_prepared_after_supplement_adoption_with_one_shared_budget(tmp_path: Path) -> None:
    from myrk.conversation import Supplement, Task
    from myrk.task_control import TaskBudget
    original = "最早原句。" + "获准长原文。\n" * 3000 + "最后原句。"
    (tmp_path / "first.txt").write_text(original, encoding="utf-8")
    (tmp_path / "second.txt").write_text("新补充要求的真实原句。", encoding="utf-8")
    scope = TaskScope(root=str(tmp_path), allowed_actions=["read"], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="读取并保留证据", scope=scope, request_policy="shared", request_limit=3)
    budget = TaskBudget(task)

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"first.txt","max_lines":1000}', tool_call_id="long-first")}
        elif len(results) == 1:
            assert "新增重要原话：再读second.txt，不覆盖先前失败证据。" in str(messages)
            assert "全文证据编号：" in str(results[0].content)
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"second.txt"}', tool_call_id="new-second")}
        else:
            yield "已按补充读取真实原文，未完事项仍保留。"

    def observe(update) -> None:
        if update.kind == "action" and update.action.status == "started" and not task.supplements:
            text = "新增重要原话：再读second.txt，不覆盖先前失败证据。"
            task.supplements.append(Supplement(text=text))

    path = tmp_path / "record.json"
    turn = asyncio.run(Conversation(path).ask(task.goal, document=None, scope=scope, task=task, budget=budget, model=FunctionModel(stream_function=respond), on_update=observe))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert turn.status == "completed" and task.requests == task.total_requests == 3 and task.active_seconds_limit is None
    assert [action.prepared_request for action in turn.actions] == [1, 2]
    assert task.supplements[0].applied_request == 2
    assert "新增重要原话" in str(saved["prepared_contexts"][1]["messages"])
    assert [item["request_attempt"] for item in saved["prepared_contexts"]] == [1, 2, 3]


def test_long_context_retrieval_keeps_time_past_old_deadline_until_explicit_stop(tmp_path: Path, monkeypatch) -> None:
    from myrk.conversation import Task
    from myrk.task_control import TaskBudget
    original = "开头" + "获准原文。\n" * 5000 + "需要回查的未完事项937。"
    document = tmp_path / "source.txt"
    document.write_text(original, encoding="utf-8")
    now = 100.0
    monkeypatch.setattr("myrk.task_control.monotonic", lambda: now)
    task = Task(goal="长资料主动运行计时", scope=None, active_seconds_limit=3)
    budget = TaskBudget(task)

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        nonlocal now
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_authorized_text", json_args="{}", tool_call_id="timed-long-read")}
        elif len(results) == 1:
            with budget.waiting():
                now += 10
                await asyncio.sleep(0)
            assert task.active_seconds == 0
            evidence_id = re.search(r"全文证据编号：([a-f0-9]{64})", str(results[0].content)).group(1)
            yield {0: DeltaToolCall(name="read_context_evidence", json_args=json.dumps({"evidence_id": evidence_id, "query": "未完事项937"}), tool_call_id="timed-read-back")}
        else:
            assert "未完事项937" in str(results[-1].content)
            now += 4
            budget.account()
            assert task.active_seconds == 4 and not budget.token.cancelled
            yield "用户等待结束，已回查原文，仍有未完事项。"
            await asyncio.Event().wait()
            yield "停止后不应继续输出。"

    path = tmp_path / "record.json"
    async def run():
        return await asyncio.wait_for(Conversation(path).ask(task.goal, document=document, task=task, budget=budget,
            model=FunctionModel(stream_function=respond), on_update=lambda event: budget.token.cancel() if event.kind == "text" else None), timeout=10)
    turn = asyncio.run(run())
    assert turn.status == "cancelled" and turn.error is None
    assert "已回查原文" in turn.answer and task.requests == 3
    assert "停止后不应继续输出" not in turn.answer and task.active_seconds == 4
    reopened = Conversation.open(path).turns[-1]
    assert reopened.task.active_seconds_limit is None and reopened.task.active_seconds == 4
    assert reopened.task.requests == reopened.task.total_requests == 3


def test_unpaired_original_tool_evidence_is_not_silently_discarded_to_continue(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    original_messages = json.loads(ModelMessagesTypeAdapter.dump_json([
        ModelRequest(parts=[ToolReturnPart("read_text", "原始失败证据：请求编号缺失，待人工核对。", tool_call_id="missing-call")]),
        ModelResponse(parts=[TextPart("待核查原始请求，不能说已成功恢复。")]),
    ]))
    path.write_text(json.dumps({"version": 3, "messages": original_messages, "turns": [{
        "prompt": "原始任务", "answer": "待核查原始请求，不能说已成功恢复。", "status": "completed",
    }]}, ensure_ascii=False), encoding="utf-8")
    received: list[list[ModelMessage]] = []

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        received.append(messages)
        yield "不应该丢掉未能配对的原始工具证据后假装可以继续。"

    turn = asyncio.run(Conversation.open(path).ask("继续核对。", document=None, model=FunctionModel(stream_function=respond)))
    assert turn.status == "failed" and "配对" in turn.error and "未发送" in turn.error
    assert received == [] and turn.requests == 0
    assert json.loads(path.read_text(encoding="utf-8"))["messages"] == original_messages
