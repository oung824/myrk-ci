"""Offline contracts for the complete request after real SDK serialization."""

import asyncio
from contextlib import asynccontextmanager
from importlib.metadata import version
import json
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext, ToolReturn, models
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.models.openai import OpenAIChatModel

from myrk.actions import Action
from myrk.conversation import Conversation, Supplement, Task, TaskScope
from myrk.context import CORE_TOOL_CONTRACT_VERSION
from myrk.extensions import ExtensionCatalog
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig
from myrk.review import review_action
from myrk.task_control import TaskBudget
import myrk.conversation as conversation_module


FIXTURE = Path(__file__).parent / "fixtures/request_contracts/core-tools-v2.json"


def assert_json(expected, actual, *, context="contract", path="$"):
    def fail(where, before, after, detail=""):
        def compact(value):
            text = json.dumps(value, ensure_ascii=False)
            return text if len(text) <= 180 else text[:177] + "..."
        raise AssertionError(f"{context}\n{where}{detail}\nexpected={compact(before)} actual={compact(after)}")

    if type(expected) is not type(actual):
        fail(path, expected, actual, f" (type {type(expected).__name__} -> {type(actual).__name__})")
    if isinstance(expected, dict):
        if expected.keys() == actual.keys() and list(expected) != list(actual):
            index = next(i for i, keys in enumerate(zip(expected, actual)) if keys[0] != keys[1])
            fail(path, list(expected), list(actual), f" (object key order at {index}: {list(expected)[index]!r} -> {list(actual)[index]!r})")
        for key in expected:
            if key not in actual:
                fail(f"{path}.{key}", expected[key], "<missing>")
            assert_json(expected[key], actual[key], context=context, path=f"{path}.{key}")
        for key in actual:
            if key not in expected:
                fail(f"{path}.{key}", "<missing>", actual[key])
    elif isinstance(expected, list):
        for index, (before, after) in enumerate(zip(expected, actual)):
            assert_json(before, after, context=context, path=f"{path}[{index}]")
        if len(expected) != len(actual):
            index = min(len(expected), len(actual))
            fail(f"{path}[{index}]", expected[index] if index < len(expected) else "<missing>",
                 actual[index] if index < len(actual) else "<missing>",
                 f" (array length {len(expected)} -> {len(actual)})")
    elif expected != actual:
        detail = ""
        if isinstance(expected, str):
            index = next((i for i, pair in enumerate(zip(expected, actual)) if pair[0] != pair[1]), min(len(expected), len(actual)))
            detail = f" (first character {index}; {expected[max(0, index - 20):index + 40]!r} -> {actual[max(0, index - 20):index + 40]!r})"
        fail(path, expected, actual, detail)


@pytest.mark.parametrize("expected,actual,path", [
    ({"tools": [{"function": {"parameters": {"type": "string"}}}]},
     {"tools": [{"function": {"parameters": {"type": "integer"}}}]}, "$.tools[0].function.parameters.type"),
    ({"messages": [{"role": "user", "content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]}]},
     {"messages": [{"role": "user", "content": [{"type": "text", "text": "first"}, {"type": "text", "text": "changed"}]}]}, "$.messages[0].content[1].text"),
    ({"enabled": 1}, {"enabled": True}, "$.enabled"),
    ({"schema": {"properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}}},
     {"schema": {"properties": {"b": {"type": "integer"}, "a": {"type": "integer"}}}}, "$.schema.properties"),
    ({"tools": [{"function": {"name": "first"}}]},
     {"tools": [{"function": {"name": "first"}}, {"function": {"name": "unexpected"}}]}, "$.tools[1]"),
])
def test_contract_failure_identifies_the_first_json_path_and_keeps_types(expected, actual, path):
    with pytest.raises(AssertionError) as failure:
        assert_json(expected, actual, context="protocol=deepseek actor=main attempt=1 core_contract=1")
    assert path in str(failure.value)
    assert "expected=" in str(failure.value) and "actual=" in str(failure.value)


def response(*, tool=None, arguments=None, text="已完成。", call_id="contract"):
    delta = {"role": "assistant"}
    if tool is not None:
        delta["tool_calls"] = [{"index": 0, "id": call_id, "type": "function",
                                "function": {"name": tool, "arguments": json.dumps(arguments or {})}}]
    else:
        delta["content"] = text
    chunk = {"id": call_id, "object": "chat.completion.chunk", "created": 1, "model": "contract-model",
             "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if tool else "stop"}]}
    return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                           headers={"content-type": "text/event-stream"})


def config(protocol):
    return ProviderConfig(name="DeepSeek" if protocol == "deepseek" else "OpenAI",
                          protocol=protocol, model="deepseek-flash" if protocol == "deepseek" else "gpt-4.1",
                          base_url="https://api.deepseek.com" if protocol == "deepseek" else "https://api.openai.com/v1")


@asynccontextmanager
async def sdk(protocol, respond, *, profile=None):
    selected = config(protocol)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        async with AsyncOpenAI(api_key="synthetic-only", base_url=selected.base_url, http_client=http, max_retries=0) as client:
            provider = DeepSeekProvider(openai_client=client) if protocol == "deepseek" else OpenAIProvider(openai_client=client)
            yield OpenAIChatModel(selected.model, provider=provider, profile=profile)


def core_fixture(case):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["fixture_format"] == 1
    assert fixture["core_contract_version"] == CORE_TOOL_CONTRACT_VERSION
    assert fixture["sdk"] == {"pydantic-ai-slim": version("pydantic-ai-slim"), "openai": version("openai")}
    return fixture["cases"][case]["tools"]


def assert_core(request, case, protocol, attempt=1):
    assert_json(core_fixture(case), request.get("tools", []), path="$.tools",
                context=f"protocol={protocol} actor={case} attempt={attempt} core_contract={CORE_TOOL_CONTRACT_VERSION}")


async def actor_requests(root, protocol):
    """Capture distinct, real assembly paths; each request still uses the same SDK."""
    root.mkdir(parents=True, exist_ok=True)
    cases, sent = {}, []
    selected = config(protocol)
    mode = "answer"

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        user = [item for item in payload["messages"] if item["role"] == "user"]
        child = any(str(item["content"]).startswith("受委派工作：") for item in user)
        if child:
            cases["delegation-scoped"] = payload
            return response()
        results = [item for item in payload["messages"] if item["role"] == "tool"]
        if mode == "delegate" and not results:
            return response(tool="delegate_work", arguments={"goal": "核对自身允许范围", "context": "只读获准任务文件夹",
                            "evidence_requirements": "说明范围", "expected_files": ["child.txt"]})
        if mode == "memory" and not results:
            return response(tool="queue_memory_review")
        if mode == "review":
            return response(text=json.dumps({"decision": "allow", "risk": "low", "authorization": "high", "rule": "general",
                            "reason": "用户明确允许本次确切动作，风险范围小且可恢复。", "same_denied_outcome": False}))
        return response()

    async with sdk(protocol, respond) as model:
        agent = Agent(model, instructions="固定契约输入。")
        ExtensionCatalog().install(agent)
        async with agent.run_stream("固定契约输入。") as result:
            await result.get_output()
        cases["bridge-only"] = sent[-1]
        for case in ["main-unscoped", "main-scoped", "main-document", "main-legacy-scoped"]:
            recipient = "deepseek" if protocol == "deepseek" else "openai-compatible"
            scope = TaskScope(root=str(root), allowed_actions=["read"], provider=recipient, recipients=[recipient]) if "scoped" in case and case != "main-unscoped" else None
            task = Task(id=f"contract-{case}", goal="核对契约", scope=scope, provider_config=selected)
            task.approval.access.legacy_scope = case == "main-legacy-scoped"
            document = None
            if case == "main-document":
                document = root / "source.txt"
                document.write_text("单份获准资料。", encoding="utf-8")
            turn = await Conversation(root / f"{case}.json").ask(task.goal, document=document, task=task, scope=scope, model=model)
            assert turn.status == "completed", turn.error
            cases[case] = sent[-1]
        mode = "delegate"
        scope = TaskScope(root=str(root), allowed_actions=["read"], provider=recipient, recipients=[recipient])
        task = Task(id="contract-delegation", goal="委派核对范围", scope=scope, provider_config=selected)
        turn = await Conversation(root / "delegation.json").ask(task.goal, document=None, task=task, scope=scope, model=model)
        assert turn.status == "completed", turn.error
        assert "delegation-scoped" in cases
        mode = "memory"
        task = Task(id="contract-memory", goal="交付后整理", scope=None, provider_config=selected)
        turn = await Conversation(root / "memory.json").ask(task.goal, document=None, task=task, model=model)
        assert turn.status == "completed", turn.error
        assert turn.request_attempts[-1].actor == "memory_review"
        cases["memory-review"] = sent[-1]
        mode = "review"
        task = Task(goal="审核确切动作", scope=scope, provider_config=selected, approval=ApprovalSettings(mode="auto_review"))
        action = Action(task_id=task.id, kind="create", target="report.txt", approval_settings=task.approval)
        allowed = await review_action(action, model=model, budget=TaskBudget(task), directory=root / "review",
                                      messages=[], denials=[], save=lambda: None, context=lambda: "确切动作已获准。")
        assert allowed and action.review.status == "allowed"
        cases["action-review"] = sent[-1]
    return cases


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat"])
def test_full_ordered_core_tools_match_the_reviewed_sdk_fixture_for_each_actor(tmp_path, monkeypatch, protocol):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    cases = asyncio.run(actor_requests(tmp_path, protocol))
    (tmp_path / "actual-requests.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    for case, request in cases.items():
        assert_core(request, case, protocol)
        expected_fields = {"model", "stream", "messages", "stream_options", "tool_choice", "tools"}
        if case not in {"bridge-only", "action-review"}:
            expected_fields.add("max_completion_tokens")
            assert request["max_completion_tokens"] == 8192
        if protocol == "deepseek" and case not in {"bridge-only", "action-review"}:
            expected_fields.add("reasoning_effort")
        assert set(request) == expected_fields, (protocol, case, set(request))
        assert request["stream_options"] == {"include_usage": True}
        assert "response_format" not in request, "The real reviewer returns validated JSON text, not an invented output tool."
        pending = [request]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                assert not {"cache_control", "cached_content", "cachePoint"}.intersection(item), (protocol, case, item)
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
    assert set(cases) == set(json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"])


async def directory_requests(root, protocol, monkeypatch):
    sent = []
    current_names = []

    def catalog_factory(**kwargs):
        catalog = ExtensionCatalog(**kwargs)
        for name in current_names:
            def echo(value: str) -> str:
                return value
            catalog.add_function(echo, name=name, description="Internal extension definition, never a top-level declaration.")
        return catalog

    monkeypatch.setattr(conversation_module, "ExtensionCatalog", catalog_factory)

    def respond(request):
        sent.append(json.loads(request.content))
        return response()

    async with sdk(protocol, respond) as model:
        for names in [[], ["alpha"], ["alpha", "beta", "gamma"], ["alpha", "beta", "gamma", "delta"], ["delta", "gamma", "beta", "alpha"]]:
            current_names = names
            task = Task(id="fixed-directory-task", goal="固定任务，只回答。", scope=None, provider_config=config(protocol))
            turn = await Conversation(root / "record.json").ask(task.goal, document=None, task=task, model=model)
            assert turn.status == "completed", turn.error
    return sent


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat"])
def test_empty_one_many_added_and_reverse_extension_catalogs_first_match_static_core_then_prefix(tmp_path, monkeypatch, protocol):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    sent = asyncio.run(directory_requests(tmp_path, protocol, monkeypatch))
    for attempt, request in enumerate(sent, 1):
        assert_core(request, "main-unscoped", protocol, attempt)
        assert_json(sent[0]["messages"], request["messages"], context=f"{protocol} directory={attempt}", path="$.messages")
        assert_json(sent[0]["tools"], request["tools"], context=f"{protocol} directory={attempt}", path="$.tools")


def test_static_contract_catches_an_accidental_top_level_tool_even_when_both_controls_change(tmp_path, monkeypatch):
    original_install = ExtensionCatalog.install

    def accidentally_register(self, agent):
        original_install(self, agent)
        @agent.tool_plain
        def accidental_low_frequency(value: str) -> str:
            """An extension accidentally registered directly in both controls."""
            return value

    monkeypatch.setattr(ExtensionCatalog, "install", accidentally_register)
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    sent = asyncio.run(directory_requests(tmp_path, "deepseek", monkeypatch))
    assert sent[0]["tools"] == sent[1]["tools"]
    for request in sent:
        with pytest.raises(AssertionError, match=r"\$\.tools\[3\]\.function\.name") as failure:
            assert_core(request, "main-unscoped", "deepseek")
        assert "accidental_low_frequency" in str(failure.value)


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
    expected_fields = {"model", "stream", "messages", "stream_options", "tool_choice", "tools", "max_completion_tokens"}
    if protocol == "deepseek":
        expected_fields.add("reasoning_effort")
    assert all(set(request) == expected_fields for request in sent)
    assert all(request["max_completion_tokens"] == 8192 for request in sent)


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
            if not observed:
                monkeypatch.setattr(conversation_module, "observe_request", lambda model, _attempt, **kwargs: model)
            recipient = "deepseek" if protocol == "deepseek" else "openai-compatible"
            scope = TaskScope(root=str(root), allowed_actions=["read"], provider=recipient, recipients=[recipient])
            task = Task(id="observation-body-contract", goal="读取note.txt原文。", scope=scope, provider_config=config(protocol))
            task.approval.revision = "fixed-approval-revision"
            async with sdk(protocol, respond) as model:
                turn = await Conversation(tmp_path / "record.json").ask(task.goal, document=None, task=task, scope=scope, model=model)
                assert turn.status == "completed", turn.error
                assert len(turn.actions) == 1 and turn.actions[0].effect == "completed"
                assert "原工具读取的真实文件" in turn.actions[0].output
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
