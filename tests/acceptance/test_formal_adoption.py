"""Only saved SDK inputs and terminal Mock responses are exercised here."""

import asyncio
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

import httpx2
import pytest
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart


MATERIAL = Path(__file__).parent / "materials/formal_adoption"


@pytest.fixture(scope="session")
def candidate():
    from tests.support.agent_acceptance import formal_adoption
    return formal_adoption


def original_exchange():
    index = json.loads((MATERIAL / "stored-source06-retry-index.json").read_bytes())
    archive = MATERIAL / index["archive"]
    assert len(archive.read_bytes()) == index["archive_bytes"]
    assert sha256(archive.read_bytes()).hexdigest() == index["archive_sha256"]
    with ZipFile(archive) as source:
        body = source.read("inputs.json")
    assert len(body) == index["files"][0]["bytes"]
    assert sha256(body).hexdigest() == index["files"][0]["sha256"]
    return json.loads(body)


async def returned_path(response):
    body = (await response.aread()).decode("utf-8")
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            for choice in json.loads(line[6:]).get("choices", []):
                for call in choice.get("delta", {}).get("tool_calls", []):
                    return json.loads(call["function"]["arguments"])["path"]
    return None


def sdk_input(messages):
    return {"messages": ModelMessagesTypeAdapter.dump_python(messages, mode="json"), "sdk_input_id": "synthetic-sdk-input"}


async def ask(mock, messages, *, purpose="work"):
    response = mock.respond(httpx2.Request("POST", "https://acceptance.invalid/chat/completions", json={"stream": True}),
        sdk_input(messages), purpose=purpose)
    return await returned_path(response)


def test_actual_supplement_retry_keeps_source06_pending_instead_of_skipping_to07(candidate):
    original = original_exchange()
    captured = {item["sdk_input_id"]: item for item in original["sdk_inputs"]}
    mock = candidate.adoption_mock("formal.compaction-adoption", "deepseek")

    async def replay():
        paths = []
        for exchange in original["exchanges"][:9]:
            sdk = captured[exchange["sdk_input_id"]]
            response = mock.respond(httpx2.Request("POST", exchange["url"], content=exchange["request_utf8"].encode("utf-8")),
                sdk, purpose=exchange["request_identity"]["purpose"])
            paths.append(await returned_path(response))
        return paths

    paths = asyncio.run(replay())
    assert paths[:8] == ["source-00.txt", "source-01.txt", "source-02.txt", "source-03.txt", "source-04.txt", "source-05.txt", None, "source-06.txt"]
    assert paths[8] == "source-06.txt"


def test_emitted_call_or_missing_return_cannot_advance_and_summary_is_independent(candidate):
    mock = candidate.adoption_mock("formal.compaction-adoption", "deepseek")

    async def exercise():
        initial = [ModelRequest(parts=[UserPromptPart("Read fixed sources.")])]
        first = await ask(mock, initial)
        pending = [*initial, ModelResponse(parts=[ToolCallPart("read_text", {"path": "source-00.txt"}, "pending")])]
        again = await ask(mock, pending)
        completed = [*pending, ModelRequest(parts=[ToolReturnPart("read_text", "Actual source.", "pending")])]
        summary = await ask(mock, completed, purpose="compaction")
        still_pending = await ask(mock, pending)
        next_source = await ask(mock, completed)
        return first, again, summary, still_pending, next_source

    assert asyncio.run(exercise()) == ("source-00.txt", "source-00.txt", None, "source-00.txt", "source-01.txt")


def test_repeated_retry_returns_do_not_queue_extra_reads_after_success_or_forget_prior_sources(candidate):
    mock = candidate.adoption_mock("formal.compaction-adoption", "deepseek")
    history: list[ModelMessage] = []
    for number in range(6):
        history.extend([ModelResponse(parts=[ToolCallPart("read_text", {"path": f"source-{number:02}.txt"}, f"old-{number}")]),
            ModelRequest(parts=[ToolReturnPart("read_text", "Actual source.", f"old-{number}")])])

    async def exercise():
        assert await ask(mock, history) == "source-06.txt"
        retried = [ModelResponse(parts=[ToolCallPart("read_text", {"path": "source-06.txt"}, "held-6")]),
            ModelRequest(parts=[ToolReturnPart("read_text", "Supplement arrived.", "held-6", outcome="retried")])]
        first_retry = await ask(mock, retried)
        second_retry = await ask(mock, retried)
        await ask(mock, [], purpose="compaction")
        fixed = [*retried, ModelResponse(parts=[ToolCallPart("read_text", {"path": "source-06.txt"}, "fixed-6")]),
            ModelRequest(parts=[ToolReturnPart("read_text", "Actual source 06.", "fixed-6")])]
        next_source = await ask(mock, fixed)
        after7 = [*fixed, ModelResponse(parts=[ToolCallPart("read_text", {"path": "source-07.txt"}, "fixed-7")]),
            ModelRequest(parts=[ToolReturnPart("read_text", "Actual source 07.", "fixed-7")])]
        return first_retry, second_retry, next_source, await ask(mock, after7)

    assert asyncio.run(exercise()) == ("source-06.txt", "source-06.txt", "source-07.txt", "source-08.txt")


@pytest.mark.parametrize("kind", ["unknown_id", "wrong_id", "wrong_tool", "write_call", "wrong_path", "retried", "success"])
def test_only_the_actual_read_call_success_can_confirm_progress(candidate, kind):
    call_name = "write_text" if kind == "write_call" else "read_text"
    path = "other/source-00.txt" if kind == "wrong_path" else "source-00.txt"
    calls = [] if kind == "unknown_id" else [ModelResponse(parts=[ToolCallPart(call_name, {"path": path}, "read-0")])]
    messages = [*calls, ModelRequest(parts=[ToolReturnPart("other" if kind == "wrong_tool" else "read_text", "source-00 claims complete",
        "different-id" if kind == "wrong_id" else "read-0", outcome="retried" if kind == "retried" else "success")])]
    mock = candidate.adoption_mock("formal.compaction-adoption", "deepseek")
    assert asyncio.run(ask(mock, messages)) == ("source-01.txt" if kind == "success" else "source-00.txt")


@pytest.mark.parametrize("outcome", ["failed", "denied", "interrupted"])
def test_non_retry_read_failures_are_reported_incomplete_without_automatic_reexecution(candidate, outcome):
    mock = candidate.adoption_mock("formal.compaction-adoption", "deepseek")
    messages = [ModelResponse(parts=[ToolCallPart("read_text", {"path": "source-00.txt"}, "failed-read")]),
        ModelRequest(parts=[ToolReturnPart("read_text", "Actual failure.", "failed-read", outcome=outcome)])]
    assert asyncio.run(ask(mock, messages)) is None


@pytest.mark.parametrize("case_id", ["formal.files", "formal.send", "formal.delegation", "formal.restart", "formal.compaction-material", "formal.compaction-request"])
def test_other_formal_cases_do_not_select_the_adoption_response_policy(candidate, case_id):
    assert candidate.adoption_mock(case_id, "deepseek") is None


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_real_sdk_shapes_keep_retried_read_and_advance_only_after_success(candidate, protocol):
    from pydantic_ai import models
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.tools import ToolDefinition
    from myrk.provider import configured_model
    from tests.support.agent_acceptance.replay import SDKCapture

    mock = candidate.adoption_mock("formal.compaction-adoption", protocol)
    captured = []
    parameters = ModelRequestParameters(function_tools=[ToolDefinition(name="read_text", description="Read only one controlled source.",
        parameters_json_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False})])

    async def exercise():
        def respond(request):
            return mock.respond(request, captured[-1], purpose="work")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False) as client:
            with models.override_allow_model_requests(True):
                async with configured_model("acceptance-synthetic-only", mock.terminal.config, http_client=client) as actual:
                    model = SDKCapture(actual, captured)
                    messages = [ModelRequest(parts=[UserPromptPart("Read each fixed source in order.")])]
                    first = await model.request(messages, None, parameters)
                    first_call = next(part for part in first.parts if isinstance(part, ToolCallPart))
                    messages.extend([first, ModelRequest(parts=[ToolReturnPart("read_text", "Supplement arrived before the read.", first_call.tool_call_id, outcome="retried")])])
                    retry = await model.request(messages, None, parameters)
                    retry_call = next(part for part in retry.parts if isinstance(part, ToolCallPart))
                    messages.extend([retry, ModelRequest(parts=[ToolReturnPart("read_text", "Actual controlled source return.", retry_call.tool_call_id)])])
                    next_reply = await model.request(messages, None, parameters)
                    next_call = next(part for part in next_reply.parts if isinstance(part, ToolCallPart))
                    return [part.args_as_dict()["path"] for part in (first_call, retry_call, next_call)]

    before = models.ALLOW_MODEL_REQUESTS
    assert asyncio.run(exercise()) == ["source-00.txt", "source-00.txt", "source-01.txt"]
    assert models.ALLOW_MODEL_REQUESTS is before
    assert len(captured) == len(mock.terminal.exchanges) == 3


def test_all_twenty_eight_sources_and_both_comparison_reads_must_succeed_before_final_reply(candidate):
    mock = candidate.adoption_mock("formal.compaction-adoption", "deepseek")
    expected = [f"source-{number:02}.txt" for number in range(28)] + ["receipt.txt", "reference.txt"]

    async def emitted(messages):
        response = mock.respond(httpx2.Request("POST", "https://acceptance.invalid/chat/completions", json={"stream": True}),
            sdk_input(messages), purpose="work")
        body = (await response.aread()).decode("utf-8")
        for line in body.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                for choice in json.loads(line[6:]).get("choices", []):
                    for call in choice.get("delta", {}).get("tool_calls", []):
                        return ToolCallPart(call["function"]["name"], json.loads(call["function"]["arguments"]), call["id"])
        return None

    async def exercise():
        messages = [ModelRequest(parts=[UserPromptPart("Read the 28 fixed sources and the explicitly adopted comparison.")])]
        observed = []
        for target in expected:
            call = await emitted(messages)
            assert call is not None and call.args_as_dict()["path"] == target
            observed.append(target)
            if target == "source-06.txt":
                messages.extend([ModelResponse(parts=[call]), ModelRequest(parts=[ToolReturnPart("read_text", "Real supplement retry.", call.tool_call_id, outcome="retried")])])
                await ask(mock, messages, purpose="compaction")
                retried = await emitted(messages)
                assert retried is not None and retried.args_as_dict()["path"] == "source-06.txt"
                observed.append("source-06.txt")
                call = retried
            messages.extend([ModelResponse(parts=[call]), ModelRequest(parts=[ToolReturnPart("read_text", "Actual source bytes.", call.tool_call_id)])])
        assert await emitted(messages) is None
        return observed

    observed = asyncio.run(exercise())
    assert observed == [*expected[:7], "source-06.txt", *expected[7:]]


@pytest.mark.parametrize("conflict", ["target", "outcome"])
def test_one_sdk_tool_call_id_cannot_change_its_target_or_outcome(candidate, conflict):
    from tests.support.agent_acceptance.core import SharedControlError

    mock = candidate.adoption_mock("formal.compaction-adoption", "deepseek")
    original = [ModelResponse(parts=[ToolCallPart("read_text", {"path": "source-00.txt"}, "same-id")]),
        ModelRequest(parts=[ToolReturnPart("read_text", "Retry.", "same-id", outcome="retried")])]
    asyncio.run(ask(mock, original))
    changed = [ModelResponse(parts=[ToolCallPart("read_text", {"path": "source-01.txt" if conflict == "target" else "source-00.txt"}, "same-id")]),
        ModelRequest(parts=[ToolReturnPart("read_text", "Changed.", "same-id", outcome="success" if conflict == "outcome" else "retried")])]
    with pytest.raises(SharedControlError, match="changed its actual"):
        asyncio.run(ask(mock, changed))


def test_the_unchanged_global_static_mock_keeps_status_interruption_and_exhaustion_semantics(candidate):
    from tests.support.agent_acceptance.core import SharedControlError
    from tests.support.agent_acceptance.replay import OfflineSession, Reply

    assert candidate.adoption_mock("formal.send", "deepseek") is None
    static = OfflineSession("deepseek", [Reply(status=503), Reply(text="after explicit retry")])
    request = httpx2.Request("POST", "https://acceptance.invalid/chat/completions", json={"stream": True})
    assert static.respond(request).status_code == 503
    second = static.respond(request)
    assert asyncio.run(second.aread())
    with pytest.raises(SharedControlError, match="script exhausted"):
        static.respond(request)
    interrupted = OfflineSession("deepseek", [Reply(text="received prefix", interrupt=True)])
    with pytest.raises(httpx2.ReadError, match="synthetic interruption after delivered output"):
        asyncio.run(interrupted.respond(request).aread())
    assert "received prefix" in interrupted.exchanges[0]["received_utf8"]
