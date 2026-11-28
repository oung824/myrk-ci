import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from pydantic_ai import models

from myrk.conversation import Conversation, ConversationRecord, Task
from myrk.provider import ProviderConfig, configured_model
from tests.support.observed_http import observed_mock_transport
from tests.test_compaction_protocols import wire_response
from tests.test_request_contracts import assert_json


FIXTURE = Path(__file__).parent / "fixtures" / "request_contracts" / "sdk-264-reopen.json"


@pytest.mark.parametrize("protocol", ["deepseek", "google"])
def test_reopen_preserves_the_request_prefix_captured_on_sdk_264(tmp_path, monkeypatch, protocol):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["sdk"] == "2.64.0"
    sample = fixture["protocols"][protocol]
    path = tmp_path / "conversation.json"
    path.write_text(json.dumps(sample["record"]), encoding="utf-8")
    legacy = ConversationRecord.model_validate(sample["record"])
    config = ProviderConfig() if protocol == "deepseek" else ProviderConfig(
        name="Gemini", protocol="google", base_url="https://generativelanguage.googleapis.com", model="gemini-2.5-flash")
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return wire_response(request, protocol, config.model, "Reopened offline reply.")

    async def run():
        task = Task(id="sdk-264-reopen", goal="Continue the check.", scope=None, provider_config=config)
        async with httpx2.AsyncClient(transport=observed_mock_transport(respond), trust_env=False, timeout=60) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                return await Conversation.open(path).ask("Continue the check.", document=None, model=model, task=task)

    turn = asyncio.run(run())
    assert turn.status == "completed", turn.error
    assert len(requests) == turn.requests == 1
    wire = requests[0]
    if protocol == "google":
        assert_json(sample["system"], wire["systemInstruction"], context="Gemini system instruction")
        history = sample["history_prefix"]
        assert_json(history, wire["contents"][:len(history)], context="Gemini history prefix")
    else:
        expected = sample["system"] + sample["history_prefix"]
        messages = wire["messages"]
        assert_json(expected, messages[:len(expected)], context="DeepSeek message prefix")
        assert messages.count(sample["system"][2]) == 1
    saved = ConversationRecord.model_validate_json(path.read_bytes())
    assert saved.messages[:len(legacy.messages)] == legacy.messages
