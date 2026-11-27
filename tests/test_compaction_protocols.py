import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, ThinkingPart, ToolCallPart, ToolReturnPart, UserPromptPart

from myrk.conversation import Conversation, ConversationRecord, Task, TaskScope
from myrk.provider import ProviderConfig, configured_model


SUMMARY = "Goal: verify 937 against originals. Constraints: read only, never invent evidence. Failed: source not found. Pending: verify original sources."


def wire_response(request, protocol: str, model_name: str, text: str, *, report_input: bool = True):
    streaming = json.loads(request.content).get("stream") is True or "streamGenerateContent" in request.url.path
    usage = {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020, "prompt_tokens_details": {"cached_tokens": 700}}
    if not report_input:
        usage = {"completion_tokens": 20}
    if protocol == "google":
        data = {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 20, "thoughtsTokenCount": 0, "cachedContentTokenCount": 700, "totalTokenCount": 1020}}
        if not report_input:
            data["usageMetadata"] = {"candidatesTokenCount": 20, "thoughtsTokenCount": 0}
    elif streaming:
        data = {"id": "controlled-stream", "object": "chat.completion.chunk", "created": 1, "model": model_name,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": usage}
    else:
        data = {"id": "controlled-summary", "object": "chat.completion", "created": 1, "model": model_name,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": usage}
    if streaming:
        return httpx2.Response(200, text=f"data: {json.dumps(data)}\n\n" + ("" if protocol == "google" else "data: [DONE]\n\n"), headers={"content-type": "text/event-stream"})
    return httpx2.Response(200, json=data)


def historical_messages(protocol: str):
    history = [ModelRequest(parts=[UserPromptPart("Original goal: verify 937; never invent evidence.")])]
    for number in range(12):
        history.extend([
            ModelResponse(parts=[TextPart(f"Evidence {number}: " + "historical information " * 180)]),
            ModelRequest(parts=[UserPromptPart(f"Continue evidence {number}.")]),
        ])
    call = ToolCallPart("read_context_evidence", {"evidence_id": "a" * 64, "start": 0, "length": 40, "query": ""}, "retained-evidence")
    parts = [call]
    if protocol == "google":
        call.provider_name = "google"
        call.provider_details = {"thought_signature": "Y2FsbA=="}
        parts.insert(0, ThinkingPart(content="", provider_name="google", provider_details={"thought_signature": "ZW1wdHk="}))
    history.extend([
        ModelResponse(parts=parts, provider_name="google" if protocol == "google" else None),
        ModelRequest(parts=[ToolReturnPart("read_context_evidence", "Original value is 937; verification remains pending.", "retained-evidence")]),
        ModelResponse(parts=[TextPart("Read the original; do not claim the task is complete.")]),
    ])
    return history


@pytest.mark.parametrize("protocol,base_url,model_name", [
    ("deepseek", "https://api.deepseek.com", "deepseek-flash"),
    ("openai-chat", "https://api.openai.com/v1", "gpt-4.1"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-2.5-flash"),
])
def test_each_protocol_compacts_and_reopens_without_losing_originals_or_tool_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str, base_url: str, model_name: str,
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    sent = []

    def respond(request):
        body = json.loads(request.content)
        is_stream = body.get("stream") is True or "streamGenerateContent" in request.url.path
        sent.append((is_stream, body))
        text = "Continue verified work." if is_stream else SUMMARY
        return wire_response(request, protocol, model_name, text)

    async def run():
        history = historical_messages(protocol)
        original = ModelMessagesTypeAdapter.dump_json(history)
        path = tmp_path / "conversation.json"
        path.write_text(ConversationRecord(version=9, messages=history, turns=[]).model_dump_json(), encoding="utf-8")
        config = ProviderConfig(name="Controlled", protocol=protocol, base_url=base_url, model=model_name,
                                context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify evidence with the original read-only constraints.", scope=None, provider_config=config)
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                conversation = Conversation.open(path)
                turn = await conversation.ask("Latest correction: read only.", document=None, task=task, model=model)
                assert turn.status == "completed", turn.error
                assert len(turn.compactions) == 1 and turn.compactions[0].status == "completed"
                assert [attempt.purpose for attempt in turn.request_attempts] == ["compaction", "work"]
                assert (turn.requests, task.turns_used) == (2, 1)
                assert (turn.input_tokens, turn.output_tokens) == (2000, 40)
                reopened = Conversation.open(path)
                restored = ConversationRecord.model_validate_json(path.read_bytes())
                assert ModelMessagesTypeAdapter.dump_json(restored.messages[:len(history)]) == original
                again = await reopened.ask("Continue with the saved summary.", document=None, task=reopened.turns[-1].task, model=model)
                assert again.status == "completed", again.error
                assert not again.compactions
                assert [attempt.purpose for attempt in again.request_attempts] == ["work"]
                assert "Evidence 0:" in reopened.read_compaction_source(turn.compactions[0].id, 1, 0)
        assert [stream for stream, _ in sent] == [False, True, True]
        assert not sent[0][1].get("tools")
        for _, body in sent[1:]:
            assert SUMMARY in json.dumps(body, ensure_ascii=False)
            assert "Latest correction: read only." in json.dumps(body, ensure_ascii=False)
            if protocol == "google":
                parts = [part for item in body["contents"] for part in item["parts"]]
                assert sum("functionCall" in part for part in parts) == sum("functionResponse" in part for part in parts) == 1
                assert any(part.get("thoughtSignature") == "Y2FsbA==" for part in parts)
                assert any(part.get("thought") and part.get("text") == "" and part.get("thoughtSignature") == "ZW1wdHk=" for part in parts)
            else:
                messages = body["messages"]
                calls = [call for item in messages for call in item.get("tool_calls", [])]
                returns = [item for item in messages if item["role"] == "tool"]
                assert [call["id"] for call in calls] == [item["tool_call_id"] for item in returns] == ["retained-evidence"]

    asyncio.run(run())


@pytest.mark.parametrize("protocol,base_url,model_name", [
    ("deepseek", "https://api.deepseek.com", "deepseek-flash"),
    ("openai-chat", "https://api.openai.com/v1", "gpt-4.1"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-2.5-flash"),
])
def test_protocol_second_compaction_inherits_earlier_sources_when_input_usage_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str, base_url: str, model_name: str,
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    sends = []

    def respond(request):
        body = json.loads(request.content)
        streaming = body.get("stream") is True or "streamGenerateContent" in request.url.path
        sends.append((streaming, body))
        return wire_response(request, protocol, model_name, "Continue verified work." if streaming else SUMMARY, report_input=not streaming)

    async def run():
        path = tmp_path / "repeated.json"
        history = historical_messages(protocol)
        original = ModelMessagesTypeAdapter.dump_json(history)
        path.write_text(ConversationRecord(version=9, messages=history, turns=[]).model_dump_json(), encoding="utf-8")
        config = ProviderConfig(name="Controlled", protocol=protocol, base_url=base_url, model=model_name,
                                context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Keep the original constraints and verify the sources.", scope=None, provider_config=config)
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                first = await Conversation.open(path).ask("Latest correction: read only.", document=None, task=task, model=model)
                assert first.status == "completed", first.error
                assert first.input_tokens is None
                old = first.compactions[0]
                stored = ConversationRecord.model_validate_json(path.read_bytes())
                for number in range(8):
                    stored.messages.extend([ModelResponse(parts=[TextPart(f"Further evidence {number}: " + "additional historical fact " * 170)]),
                                            ModelRequest(parts=[UserPromptPart(f"Keep researching {number}.")])])
                path.write_text(stored.model_dump_json(), encoding="utf-8")
                second = await Conversation.open(path).ask("Keep the latest correction and continue.", document=None, task=stored.turns[-1].task, model=model)
                assert second.status == "completed", second.error
                assert second.input_tokens is None
                assert len(second.compactions) == 1
                latest = second.compactions[0]
                assert latest.status == "completed" and latest.replaced_id == old.id
                assert {ref.key for ref in old.covered} <= {ref.key for ref in latest.covered}
                reopened = Conversation.open(path)
                assert "Evidence 0:" in reopened.read_compaction_source(latest.id, 1, 0)
                again = await reopened.ask("Continue after the second saved summary.", document=None, task=reopened.turns[-1].task, model=model)
                assert again.status == "completed" and not again.compactions, again.error
        assert [streaming for streaming, _ in sends] == [False, True, False, True, True]
        restored = ConversationRecord.model_validate_json(path.read_bytes())
        assert ModelMessagesTypeAdapter.dump_json(restored.messages[:len(history)]) == original

    asyncio.run(run())


@pytest.mark.parametrize("protocol,base_url,model_name", [
    ("deepseek", "https://api.deepseek.com", "deepseek-flash"),
    ("openai-chat", "https://api.openai.com/v1", "gpt-4.1"),
    ("google", "https://generativelanguage.googleapis.com", "gemini-2.5-flash"),
])
def test_protocol_overflow_recovers_once_in_the_original_work_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str, base_url: str, model_name: str,
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    sends = []

    def respond(request):
        body = json.loads(request.content)
        streaming = body.get("stream") is True or "streamGenerateContent" in request.url.path
        sends.append((streaming, body))
        if len(sends) == 1:
            if protocol == "google":
                return httpx2.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "The input token count (40100) exceeds the maximum number of tokens allowed (40000)"}})
            return httpx2.Response(400, json={"error": {"type": "invalid_request_error", "code": "context_length_exceeded", "message": "This request exceeds the context window."}})
        return wire_response(request, protocol, model_name, "Continue verified work." if streaming else SUMMARY)

    async def run():
        path = tmp_path / "overflow.json"
        history = historical_messages(protocol)
        original = ModelMessagesTypeAdapter.dump_json(history)
        path.write_text(ConversationRecord(version=9, messages=history, turns=[]).model_dump_json(), encoding="utf-8")
        config = ProviderConfig(name="Controlled", protocol=protocol, base_url=base_url, model=model_name,
                                context_window_override=40000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify evidence without repeating actions.", scope=None, max_turns=1, provider_config=config)
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation.open(path).ask("Continue the original work.", document=None, task=task, model=model)
        assert turn.status == "completed", turn.error
        assert task.turns_used == 1
        assert turn.requests == task.total_requests == 3
        assert [attempt.purpose for attempt in turn.request_attempts] == ["work", "compaction", "work"]
        assert [attempt.status for attempt in turn.request_attempts] == ["failed", "completed", "completed"]
        assert [streaming for streaming, _ in sends] == [True, False, True]
        assert not turn.actions
        reopened = ConversationRecord.model_validate_json(path.read_bytes())
        assert ModelMessagesTypeAdapter.dump_json(reopened.messages[:len(history)]) == original
        assert "Continue the original work." in json.dumps(sends[-1][1], ensure_ascii=False)

    asyncio.run(run())


def test_compacted_extension_description_can_be_retrieved_and_used_through_the_original_action_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    folder = tmp_path / "work"
    folder.mkdir()
    table = folder / "amounts.csv"
    table.write_text("amount\n1.25\n2.50\n", encoding="utf-8")
    phase = ["describe"]
    sent = []
    described_versions = []

    def respond(request):
        body = json.loads(request.content)
        sent.append((phase[0], body))
        if body.get("stream") is not True:
            return wire_response(request, "deepseek", "deepseek-flash", "Goal: calculate the authorized CSV total. Exact tool definitions must be obtained again when needed. Original research and constraints remain available.")
        messages = body["messages"]
        description_id = "schema-before" if phase[0] == "describe" else "schema-after"
        descriptions = [json.loads(item["content"]) for item in messages if item["role"] == "tool" and item.get("tool_call_id") == description_id]
        if not descriptions:
            if phase[0] == "calculate":
                assert described_versions[0] not in json.dumps(body, ensure_ascii=False)
            name, arguments, call_id = "describe_extension", {"name": "calculate_table"}, description_id
        elif phase[0] == "describe":
            described_versions.append(descriptions[-1]["schema_version"])
            return wire_response(request, "deepseek", "deepseek-flash", "Definition ready; computation is pending.")
        elif not any(item["role"] == "tool" and item.get("tool_call_id") == "calculate-once" for item in messages):
            described_versions.append(descriptions[-1]["schema_version"])
            name, arguments, call_id = "call_extension", {"name": "calculate_table", "schema_version": descriptions[-1]["schema_version"],
                "arguments": {"path": "amounts.csv", "column": "amount"}}, "calculate-once"
        else:
            assert any("3.75" in item["content"] for item in messages if item["role"] == "tool" and item.get("tool_call_id") == "calculate-once")
            return wire_response(request, "deepseek", "deepseek-flash", "Verified total: 3.75.")
        data = {"id": call_id, "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash",
                "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "finish_reason": "tool_calls"}]}
        return httpx2.Response(200, text=f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async def run():
        path = tmp_path / "records" / "extensions.json"
        config = ProviderConfig(context_window_override=40000, output_token_reserve=1024, context_safety_margin=512)
        scope = TaskScope(root=str(folder), allowed_actions=["read", "calculate"], provider="deepseek", recipients=["deepseek"])
        task = Task(goal="Research the inputs, then calculate the authorized table without modifying it.", scope=scope, provider_config=config)
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                first = await Conversation(path).ask("Obtain the table tool definition for later.", document=None, task=task, scope=scope, model=model)
                assert first.status == "completed", first.error
                assert not first.actions and len(described_versions) == 1
                stored = ConversationRecord.model_validate_json(path.read_bytes())
                for number in range(12):
                    stored.messages.extend([ModelResponse(parts=[TextPart(f"Research {number}: " + "historical information " * 170)]),
                                            ModelRequest(parts=[UserPromptPart(f"Continue research {number}.")])])
                path.write_text(stored.model_dump_json(), encoding="utf-8")
                phase[0] = "calculate"
                task = stored.turns[-1].task
                task.provider_config = config.model_copy(update={"context_window_override": 17000})
                second = await Conversation.open(path).ask("Use a fresh definition to calculate the table now.", document=None, task=task, scope=scope, model=model)
        assert second.status == "completed", second.error
        assert second.answer == "Verified total: 3.75."
        assert second.compactions and second.compactions[0].status == "completed"
        assert len(described_versions) == 2 and described_versions[0] == described_versions[1]
        assert len(second.actions) == 1
        action = second.actions[0]
        assert (action.kind, action.status, action.effect, action.tool_name, action.tool_source) == ("calculate", "completed", "completed", "calculate_table", "extension")
        assert action.tool_schema_version == described_versions[1]
        assert action.tool_arguments == {"path": "amounts.csv", "column": "amount"}
        assert table.read_text(encoding="utf-8") == "amount\n1.25\n2.50\n"
        assert all("calculate_table" not in [tool["function"]["name"] for tool in body.get("tools", [])] for _, body in sent)

    asyncio.run(run())
