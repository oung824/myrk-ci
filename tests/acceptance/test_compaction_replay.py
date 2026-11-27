"""External SDK boundary needed by non-streaming summary requests."""

import asyncio
import json

import pytest
from pydantic_ai.exceptions import ModelContextWindowExceededError
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from tests.support.agent_acceptance.replay import OfflineSession, Reply


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_summary_reply_keeps_native_finish_reason_and_exact_response(protocol):
    async def run():
        session = OfflineSession(protocol, [Reply(text="Incomplete summary.", finish_reason="length")])
        async with session.model() as model:
            response = await model.request([ModelRequest(parts=[UserPromptPart("Summarize material.")])], None, ModelRequestParameters())
        assert response.finish_reason == "length"
        assert response.text == "Incomplete summary."
        body = json.loads(session.exchanges[0]["received_utf8"])
        if protocol == "google":
            assert body["candidates"][0]["finishReason"] == "MAX_TOKENS"
        else:
            assert body["choices"][0]["finish_reason"] == "length"
        assert len(session.sdk_inputs) == 1
    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_window_rejection_uses_real_sdk_error_translation(protocol):
    async def run():
        session = OfflineSession(protocol, [Reply(window_error=True)])
        async with session.model() as model:
            with pytest.raises(ModelContextWindowExceededError):
                await model.request([ModelRequest(parts=[UserPromptPart("Original request.")])], None, ModelRequestParameters())
        assert session.exchanges[0]["status"] == 400
        assert "exceed" in session.exchanges[0]["received_utf8"]
    asyncio.run(run())
