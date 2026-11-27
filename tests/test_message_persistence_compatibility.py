import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from pydantic import ValidationError
from pydantic_ai import models
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from myrk.actions import Action
from myrk.context import PreparedContext
from myrk.conversation import Conversation, ConversationRecord, Task, Turn
from myrk.provider import ProviderConfig, configured_model
from myrk.review import ReviewRecord


def test_google_text_usage_survives_real_sdk_save_and_reopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sends = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sends.append(request)
        chunk = {
            "candidates": [{"content": {"role": "model", "parts": [{"text": "saved text"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5,
                              "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 3}]},
        }
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\n", headers={"content-type": "text/event-stream"})

    async def run() -> None:
        config = ProviderConfig(name="Gemini", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash", protocol="google")
        path = tmp_path / "conversation.json"
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                turn = await Conversation(path).ask("Save this response", document=None, model=model,
                    task=Task(goal="Save this response", scope=None, provider_config=config))
        assert turn.status == "completed", turn.error
        assert len(sends) == turn.requests == 1
        before = json.loads(path.read_text(encoding="utf-8"))
        assert before["messages"][-1]["usage"]["input_text_tokens"] == 3
        reopened = Conversation.open(path)
        reopened.save()
        reopened_again = Conversation.open(path)
        assert reopened_again.turns[-1].answer == "saved text"
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["messages"] == before["messages"]
        assert after["turns"] == before["turns"]
        assert after["prepared_contexts"] == before["prepared_contexts"]

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["google", "deepseek", "openai"])
def test_all_stored_message_locations_preserve_usage_signatures_and_binary_content(tmp_path: Path, provider: str) -> None:
    messages = [
        ModelRequest(parts=[UserPromptPart(["binary fixture", BinaryContent(data=b"\x00\xff\x80", media_type="application/octet-stream")])]),
        ModelResponse(parts=[ThinkingPart(content="", signature="opaque-signature", provider_name=provider,
                provider_details={"thought_signature": "signed-thinking"}),
            ToolCallPart("read_text", {"path": "input.txt"}, tool_call_id="read-1",
                provider_details={"thought_signature": "signed-call"})], provider_name=provider,
            usage=RequestUsage(input_tokens=3, output_tokens=2, input_text_tokens=3, future_modality_tokens=7)),
        ModelRequest(parts=[ToolReturnPart("read_text", "fixture content", tool_call_id="read-1")]),
        ModelResponse(parts=[TextPart("complete")], provider_name=provider),
    ]
    review = ReviewRecord(status="failed", messages=messages)
    action = Action(task_id="original-task", kind="create", target="result.txt", status="failed", review=review)
    task = Task(id="original-task", goal="retain original task", scope=None,
                request_policy="shared", request_limit=10, requests=6, total_requests=6)
    record = ConversationRecord(version=7, messages=messages,
        turns=[Turn(prompt=task.goal, answer="partial", status="failed", task=task, actions=[action], memory_review_messages=messages)],
        prepared_contexts=[PreparedContext(turn_index=0, messages=messages, instructions=[], tools=[], settings=None, omitted=[])])
    path = tmp_path / "conversation.json"
    path.write_text(record.model_dump_json(), encoding="utf-8")
    expected = ModelMessagesTypeAdapter.dump_python(messages, mode="json")

    reopened = Conversation.open(path)
    reopened.save()
    saved = ConversationRecord.model_validate_json(path.read_bytes())
    locations = [saved.messages, saved.turns[0].memory_review_messages,
                 saved.turns[0].actions[0].review.messages, saved.prepared_contexts[0].messages]
    for location in locations:
        assert ModelMessagesTypeAdapter.dump_python(location, mode="json") == expected
    assert saved.turns[0].task.id == "original-task"
    assert saved.turns[0].task.requests == saved.turns[0].task.total_requests == 6
    assert saved.turns[0].task.request_limit == 10
    assert saved.turns[0].actions[0].review.status == "failed"
    assert saved.turns[0].actions[0].effect == "not_started"


@pytest.mark.parametrize("damage", ["record_extra", "part_kind", "token_type"])
def test_message_adapter_keeps_record_and_message_validation_strict(tmp_path: Path, damage: str) -> None:
    messages = [ModelResponse(parts=[TextPart("fixture")], usage=RequestUsage(input_tokens=3, input_text_tokens=3))]
    raw = json.loads(ConversationRecord(version=7, messages=messages, turns=[]).model_dump_json())
    if damage == "record_extra":
        raw["unrecognized_record_setting"] = True
    elif damage == "part_kind":
        raw["messages"][0]["parts"][0]["part_kind"] = "not-a-message-part"
    else:
        raw["messages"][0]["usage"]["input_tokens"] = {"invalid": 3}
    path = tmp_path / "invalid.json"
    original = json.dumps(raw).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(ValidationError):
        Conversation.open(path)
    assert path.read_bytes() == original


def test_legacy_token_aliases_and_empty_details_still_reopen(tmp_path: Path) -> None:
    raw = {"version": 7, "messages": [{"kind": "response", "parts": [{"part_kind": "text", "content": "legacy"}],
        "usage": {"request_tokens": None, "response_tokens": 2, "details": None, "input_text_tokens": 0}}], "turns": []}
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    conversation = Conversation.open(path)
    conversation.save()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["messages"][0]["usage"]["input_tokens"] == 0
    assert saved["messages"][0]["usage"]["output_tokens"] == 2
    assert saved["messages"][0]["usage"]["input_text_tokens"] == 0
