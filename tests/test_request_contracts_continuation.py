import asyncio
from hashlib import sha256
import json
from uuid import UUID

import httpx2
import pytest
from pydantic_ai import Agent, RunContext, ToolReturn, models

from myrk.conversation import Conversation, Supplement, Task, TaskScope
from myrk.extensions import ExtensionCatalog
import myrk.conversation as conversation_module
from tests.test_request_contracts import assert_core, assert_json, config, response, sdk


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat"])
def test_tool_retry_supplement_and_reopening_preserve_every_previously_sent_message(tmp_path, monkeypatch, protocol):
    root = tmp_path / "task"
    root.mkdir()
    (root / "note.txt").write_text("获准文件原话937。", encoding="utf-8")
    recipient = "deepseek" if protocol == "deepseek" else "openai-compatible"
    scope = TaskScope(root=str(root), allowed_actions=["read"], provider=recipient, recipients=[recipient])
    task = Task(id="contract-retry-task", goal="读取原话后继续", scope=scope, provider_config=config(protocol))
    sent = []
    supplement = "运行中补充：只引用原话，不推测。"

    def respond(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return response(tool="read_text", arguments={"path": "note.txt"}, call_id="read")
        if len(sent) == 2:
            task.supplements.append(Supplement(text=supplement))
            return httpx2.Response(503, json={"error": {"message": "synthetic retry", "type": "temporary"}})
        return response()

    async def scenario():
        async with sdk(protocol, respond) as model:
            path = tmp_path / "record.json"
            first = await Conversation(path).ask(task.goal, document=None, task=task, scope=scope, model=model)
            assert first.status == "completed", first.error
            assert [attempt.status for attempt in first.request_attempts] == ["completed", "failed", "completed"]
            assert task.requests == 3 and task.turns_used == 2
            original_messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
            reopened = Conversation.open(path)
            second = await reopened.ask("继续同一项任务。", document=None, task=reopened.turns[-1].task, scope=scope, model=model)
            assert second.status == "completed", second.error
            assert second.task.requests == second.task.total_requests == 4
            assert second.task.turns_used == 3
            saved = json.loads(path.read_text(encoding="utf-8"))
            assert_json(original_messages, saved["messages"][:len(original_messages)], context=f"{protocol} reopened original history", path="$.messages")
            assert sum(part.get("content") == supplement for message in saved["messages"] for part in message["parts"]) == 1
            layout = saved["request_layouts"]["main"]
            assert [change["reasons"] for change in layout["changes"]] == [["new_task"]]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(scenario())
    assert len(sent) == 4
    for attempt, request in enumerate(sent, 1):
        assert_core(request, "main-scoped", protocol, attempt)
    for attempt, (previous, current) in enumerate(zip(sent, sent[1:]), 2):
        assert_json(previous["messages"], current["messages"][:len(previous["messages"])],
                    context=f"{protocol} main attempt={attempt}", path="$.messages")
    assert [message.get("content") for message in sent[-1]["messages"]].count(supplement) == 1
    assert any(message["role"] == "tool" and "获准文件原话937" in str(message["content"]) for message in sent[-1]["messages"])
    limit = "max_tokens" if protocol == "deepseek" else "max_completion_tokens"
    expected_fields = {"model", "stream", "messages", "stream_options", "tool_choice", "tools", limit}
    if protocol == "deepseek":
        expected_fields.add("reasoning_effort")
    assert all(set(request) == expected_fields for request in sent)
    assert all(request[limit] == 8192 for request in sent)


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat"])
def test_real_tool_return_retains_sdk_message_roles_and_ordered_content_blocks(tmp_path, monkeypatch, protocol):
    sent = []
    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            return response(tool="describe_extension", arguments={"name": "read_report"}, call_id="describe")
        if len(sent) == 2:
            definition = json.loads(next(item["content"] for item in payload["messages"] if item["role"] == "tool"))
            return response(tool="call_extension", arguments={"name": "read_report", "schema_version": definition["schema_version"], "arguments": {}}, call_id="read-blocks")
        return response()

    async def scenario():
        async with sdk(protocol, respond) as model:
            agent = Agent(model, instructions="固定资料读取。")
            catalog = ExtensionCatalog()
            @catalog.tool
            def read_report(ctx: RunContext[None]) -> ToolReturn:
                return ToolReturn(return_value="真实返回值937", content=["第一内容块", "第二内容块"])
            catalog.install(agent)
            async with agent.run_stream("读取资料。") as result:
                await result.get_output()
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(scenario())
    (tmp_path / "blocks-actual.json").write_text(json.dumps(sent, ensure_ascii=False, indent=2), encoding="utf-8")
    assert_json([
        {"role": "tool", "tool_call_id": "read-blocks", "content": "真实返回值937"},
        {"role": "user", "content": [{"text": "第一内容块", "type": "text"}, {"text": "第二内容块", "type": "text"}]},
    ], sent[-1]["messages"][-2:], context=f"{protocol} ToolReturn", path="$.messages[-2:]")
    for request in sent:
        assert_core(request, "bridge-only", protocol)


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat"])
def test_sdk_capacity_rebuild_is_explicit_persistent_and_keeps_original_evidence(tmp_path, monkeypatch, protocol):
    original = "开头原句。" + "中间获准原文。" * 4000 + "最后待核对937。"
    source = tmp_path / "source.txt"
    source.write_text(original, encoding="utf-8")
    path = tmp_path / "record.json"
    sent = []
    evidence_id = ""

    def respond(request):
        nonlocal evidence_id
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            return response(tool="read_authorized_text", call_id="long-source")
        if len(sent) == 3:
            import re
            evidence_id = re.search(r"全文证据编号：([a-f0-9]{64})", str(payload)).group(1)
            return response(tool="read_context_evidence", arguments={"evidence_id": evidence_id, "query": "中间获准原文", "length": 25}, call_id="recall")
        return response()

    async def scenario():
        selected = config(protocol).model_copy(update={"auto_compaction": False})
        task = Task(id="capacity-contract", goal="读取原始证据。", document=str(source), scope=None, provider_config=selected)
        async with sdk(protocol, respond) as model:
            first = await Conversation(path).ask(task.goal, document=source, task=task, model=model)
            assert first.status == "completed", first.error
            assert not first.compactions
            first_version = first.request_attempts[-1].observation.layout_version
        reopened = Conversation.open(path)
        async with sdk(protocol, respond, profile={"context_window": 16000}) as constrained:
            second = await reopened.ask("继续核对已读资料，回查原话。", document=source, task=reopened.turns[-1].task, model=constrained)
            assert second.status == "completed", second.error
            assert not second.compactions
            constrained_inputs = [item for item in json.loads(path.read_text(encoding="utf-8"))["prepared_contexts"]
                                  if item["turn_index"] == 1]
            assert constrained_inputs and all(item["accepted"] and item["input_budget_tokens"] == 6784
                                              and item["input_tokens"] <= 6784 for item in constrained_inputs)
            observation = second.request_attempts[0].observation
            assert observation.layout_version == first_version + 1
            assert observation.wire.change_reasons == ["capacity_compression"]
        reopened = Conversation.open(path)
        async with sdk(protocol, respond) as model:
            third = await reopened.ask("继续按同一份证据回答。", document=source, task=reopened.turns[-1].task, model=model)
            assert third.status == "completed", third.error
            assert not third.compactions
            assert third.request_attempts[0].observation.layout_version == observation.layout_version
        raw = json.loads(path.read_text(encoding="utf-8"))
        layout = raw["request_layouts"]["main"]
        assert [change["reasons"] for change in layout["changes"]] == [["new_task"], ["capacity_compression"]]
        assert [part["content"] for message in raw["messages"] for part in message["parts"]
                if part["part_kind"] == "tool-return" and part["tool_name"] == "read_authorized_text"] == [original]
        assert layout["previews"][evidence_id]["edge"] == 1000

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(scenario())
    first_preview = next(item for item in sent[1]["messages"] if item["role"] == "tool")
    compact = next(item for item in sent[2]["messages"] if item["role"] == "tool")
    assert "各4000个字符" in first_preview["content"] and "各1000个字符" in compact["content"]
    for request in sent[2:]:
        assert_json(compact, next(item for item in request["messages"] if item["role"] == "tool"),
                    context=f"{protocol} compact preview", path="$.messages[tool:long-source]")
    assert any(item["role"] == "tool" and item.get("tool_call_id") == "recall" and "中间获准原文" in item["content"] for item in sent[-1]["messages"])
    assert all("中间获准原文" not in str(item["content"]) for item in sent[-1]["messages"] if item["role"] == "system")
    for request in sent:
        assert_core(request, "main-document", protocol)


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat"])
def test_observation_does_not_change_complete_conversation_tool_step_request_bytes(tmp_path, monkeypatch, protocol):
    monkeypatch.setattr("myrk.actions.uuid4", lambda: UUID(int=937))
    root = tmp_path / "task"
    root.mkdir()
    (root / "note.txt").write_text("原工具读取的真实文件。", encoding="utf-8")
    runs = []
    active = []

    def respond(request):
        active.append(request.content)
        if len(active) == 1:
            return response(tool="read_text", arguments={"path": "note.txt"}, call_id="read")
        return response()

    async def scenario():
        nonlocal active
        for observed in [True, False]:
            active = []
            recipient = "deepseek" if protocol == "deepseek" else "openai-compatible"
            scope = TaskScope(root=str(root), allowed_actions=["read"], provider=recipient, recipients=[recipient])
            task = Task(id="observation-body-contract", goal="读取note.txt原文。", scope=scope, provider_config=config(protocol))
            task.approval.revision = "fixed-approval-revision"
            async with sdk(protocol, respond, observed=observed) as model:
                turn = await Conversation(tmp_path / "record.json").ask(task.goal, document=None, task=task, scope=scope, model=model)
                assert turn.status == "completed", turn.error
                assert len(turn.actions) == 1 and turn.actions[0].effect == "completed"
                assert "原工具读取的真实文件" in turn.actions[0].output
                assert len(turn.request_attempts) == len(active) == 2
                for attempt, body in zip(turn.request_attempts, active):
                    assert attempt.observation is not None
                    wire = attempt.observation.wire
                    if observed:
                        assert wire is not None and wire.body_sha256 == sha256(body).hexdigest()
                    else:
                        assert wire is None
            runs.append(active)
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(scenario())
    assert len(runs[0]) == len(runs[1]) == 2
    for attempt, (observed, plain) in enumerate(zip(*runs), 1):
        assert_json(json.loads(plain), json.loads(observed), context=f"{protocol} observed attempt={attempt}")
        assert observed == plain


def test_changed_core_definition_records_exact_tool_and_contract_versions_across_reopening(tmp_path, monkeypatch):
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return response()
    original_install = ExtensionCatalog.install

    async def scenario():
        task = Task(id="core-upgrade-contract", goal="继续原任务。", scope=None)
        path = tmp_path / "record.json"
        async with sdk("deepseek", respond) as model:
            monkeypatch.setattr(conversation_module, "CORE_TOOL_CONTRACT_VERSION", 1)
            first = await Conversation(path).ask(task.goal, document=None, task=task, model=model)
            assert first.status == "completed", first.error

            def upgraded_install(self, agent):
                original_install(self, agent)
                @agent.tool_plain
                def approved_core_v2(path: str, limit: int = 5) -> str:
                    """A synthetic, explicitly approved core definition upgrade."""
                    return path
            monkeypatch.setattr(ExtensionCatalog, "install", upgraded_install)
            monkeypatch.setattr(conversation_module, "CORE_TOOL_CONTRACT_VERSION", 2, raising=False)
            reopened = Conversation.open(path)
            second = await reopened.ask("继续并采用新定义。", document=None, task=reopened.turns[-1].task, model=model)
            assert second.status == "completed", second.error
            observation = second.request_attempts[0].observation
            assert observation.layout_version == first.request_attempts[0].observation.layout_version + 1
            assert observation.wire.change_reasons == ["core_tools_changed"]
            raw = json.loads(path.read_text(encoding="utf-8"))
            assert raw["turns"][0]["requests"] == 1
            assert raw["turns"][-1]["task"]["requests"] == 2
            assert "继续原任务" in str(sent[1]["messages"])
            layout = raw["request_layouts"]["main"]
            assert layout["core_contract_version"] == 2
            change = layout["changes"][-1]
            assert (change["previous_core_version"], change["core_version"], change["changed_tools"]) == (1, 2, ["approved_core_v2"])
            reopened = Conversation.open(path)
            third = await reopened.ask("再次续办。", document=None, task=reopened.turns[-1].task, model=model)
            assert third.status == "completed", third.error
            assert third.request_attempts[0].observation.layout_version == observation.layout_version
            assert third.request_attempts[0].observation.wire.change_reasons == []
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(scenario())
    assert_core(sent[0], "main-unscoped", "deepseek")
    assert "approved_core_v2" in [item["function"]["name"] for item in sent[1]["tools"]]
    assert_json(sent[1]["tools"], sent[2]["tools"], path="$.tools")


def test_parameter_object_order_change_is_detected_in_real_sdk_and_persisted_with_exact_tool(tmp_path, monkeypatch):
    sent = []
    def first(a: int = 1, b: int = 2) -> int:
        return a + b
    def second(b: int = 2, a: int = 1) -> int:
        return a + b
    selected = first
    original_install = ExtensionCatalog.install
    def install(self, agent):
        original_install(self, agent)
        agent.tool_plain(selected, name="ordered_core", description="Ordered core parameters.")
    def respond(request):
        sent.append(json.loads(request.content))
        return response()

    async def scenario():
        nonlocal selected
        task = Task(id="object-order-contract", goal="保留请求表示顺序。", scope=None)
        path = tmp_path / "record.json"
        async with sdk("deepseek", respond) as model:
            initial = await Conversation(path).ask(task.goal, document=None, task=task, model=model)
            assert initial.status == "completed", initial.error
            selected = second
            reopened = Conversation.open(path)
            changed = await reopened.ask("继续同一项工作。", document=None, task=reopened.turns[-1].task, model=model)
            assert changed.status == "completed", changed.error
            assert changed.request_attempts[0].observation.layout_version == initial.request_attempts[0].observation.layout_version + 1
            assert changed.request_attempts[0].observation.wire.change_reasons == ["core_tools_changed"]
            change = json.loads(path.read_text(encoding="utf-8"))["request_layouts"]["main"]["changes"][-1]
            assert change["changed_tools"] == ["ordered_core"]
            reopened = Conversation.open(path)
            same = await reopened.ask("再次续办。", document=None, task=reopened.turns[-1].task, model=model)
            assert same.status == "completed", same.error
            assert same.request_attempts[0].observation.wire.change_reasons == []
    monkeypatch.setattr(ExtensionCatalog, "install", install)
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(scenario())
    assert sent[0]["tools"] == sent[1]["tools"], "Python equality hides the serialized schema order change."
    with pytest.raises(AssertionError, match="object key order") as failure:
        assert_json(sent[0]["tools"], sent[1]["tools"], path="$.tools")
    assert ".function.parameters.properties" in str(failure.value)
