"""Offline contracts for the complete request after real SDK serialization."""

import asyncio
from contextlib import asynccontextmanager
from importlib.metadata import version
import json
from pathlib import Path

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent, models
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.models.openai import OpenAIChatModel

from myrk.actions import Action
from myrk.conversation import Conversation, Task, TaskScope
from myrk.context import CORE_TOOL_CONTRACT_VERSION
from myrk.extensions import ExtensionCatalog
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig, configured_model
from myrk.review import review_action
from myrk.task_control import TaskBudget
import myrk.conversation as conversation_module
from tests.support.observed_http import observed_mock_transport


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
async def sdk(protocol, respond, *, profile=None, observed=True):
    selected = config(protocol)
    transport = observed_mock_transport(respond) if observed else httpx2.MockTransport(respond)
    async with httpx2.AsyncClient(transport=transport) as http:
        if profile is None:
            async with configured_model("synthetic-only", selected, http_client=http) as model:
                yield model
            return
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
            limit = "max_tokens" if protocol == "deepseek" else "max_completion_tokens"
            expected_fields.add(limit)
            assert request[limit] == 8192
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
