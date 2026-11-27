"""Public persistence and usage contracts for full rendered-input measurement."""

import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from pydantic_ai import Agent, models
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from myrk.compaction import ContextLimitExceeded, WindowUsage, part_hash
from myrk.context import CurrentContext, RequestLayout
from myrk.conversation import Conversation, ConversationRecord, Task
from myrk.provider import ProviderConfig, configured_model


def test_reopened_low_usage_anchor_cannot_bypass_the_full_input_budget(tmp_path: Path) -> None:
    async def run() -> None:
        instructions = "Read only. " + "甲" * 4000
        config = ProviderConfig(auto_compaction=False, context_window_override=100000,
                                output_token_reserve=128, context_safety_margin=64)
        captures = []

        def respond(messages, info):
            captures.append(info.instructions)
            return ModelResponse(parts=[TextPart("Recorded.")])

        model = FunctionModel(respond, profile={"context_window": 100000})
        first_prepared = []
        layout = RequestLayout(task_id="preserved-task", instructions=instructions)
        current = CurrentContext(tmp_path / "record.json", 0, first_prepared, [], lambda _: None, config=config)
        current.layout = layout
        result = await Agent(model, instructions=instructions, capabilities=[current]).run("Report.")
        history = result.all_messages()
        layout.window_usage = WindowUsage(request_key=first_prepared[-1].request_key,
            part_hashes=[part_hash(part) for message in history for part in message.parts
                         if isinstance(part, (UserPromptPart, TextPart))], tokens=1)
        path = tmp_path / "saved-layout.json"
        path.write_text(layout.model_dump_json(), encoding="utf-8")
        restored = RequestLayout.model_validate_json(path.read_bytes())
        assert restored.window_usage is not None and restored.window_usage.tokens == 1

        prepared = []
        reopened = CurrentContext(tmp_path / "record.json", 1, prepared, [], lambda _: None,
                                  config=config.model_copy(update={"context_window_override": 3000}))
        reopened.layout = restored
        with pytest.raises(ContextLimitExceeded, match="资料容量不足"):
            await Agent(model, instructions=instructions, capabilities=[reopened]).run("Continue.", message_history=history)
        assert len(captures) == 1
        assert prepared[-1].accepted is False
        assert prepared[-1].measurement_source == "sdk_rendered_local_text_estimate"
        assert restored.window_usage is None

    asyncio.run(run())


@pytest.mark.parametrize("thinking,reported,cached", [(0, True, 0), (3, True, 7000),
                                                    (None, True, 7000), (None, False, None)],
                         ids=["reasoning-zero", "reasoning-in-output", "reasoning-unknown", "usage-unknown"])
def test_reported_usage_stays_recorded_without_a_new_capacity_anchor(tmp_path: Path, monkeypatch,
                                                                    thinking: int | None, reported: bool,
                                                                    cached: int | None) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        chunk = {
            "id": "synthetic", "object": "chat.completion.chunk", "created": 1,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Recorded.",
                                                   "reasoning_content": "The material was read."},
                         "finish_reason": "stop"}],
        }
        if reported:
            chunk["usage"] = {"prompt_tokens": 12000, "completion_tokens": 8, "total_tokens": 12008,
                              "prompt_cache_hit_tokens": cached, "prompt_cache_miss_tokens": 12000 - cached}
            if thinking is not None:
                chunk["usage"]["completion_tokens_details"] = {"reasoning_tokens": thinking}
        return httpx2.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                               headers={"content-type": "text/event-stream"})

    async def run() -> None:
        config = ProviderConfig(context_window_override=128000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Report local material only.", scope=None, provider_config=config)
        path = tmp_path / "conversation.json"
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with configured_model("synthetic-only", config, http_client=http) as model:
                first = await Conversation(path).ask(task.goal, document=None, task=task, model=model)
                assert first.status == "completed", first.error
                saved = ConversationRecord.model_validate_json(path.read_bytes())
                assert saved.request_layouts["main"].window_usage is None
                reopened = Conversation.open(path)
                second = await reopened.ask("Continue the same report.", document=None,
                                            task=reopened.turns[-1].task, model=model)
                assert second.status == "completed", second.error
                saved = ConversationRecord.model_validate_json(path.read_bytes())

        assert len(sent) == 2
        assert any(message.get("reasoning_content") == "The material was read."
                   for message in sent[1]["messages"] if message["role"] == "assistant")
        assert saved.request_layouts["main"].window_usage is None
        assert all(prepared.measurement_source == "sdk_rendered_local_text_estimate"
                   for prepared in saved.prepared_contexts)
        assert saved.turns[-1].task.total_requests == 2
        for turn in saved.turns:
            assert (turn.input_tokens, turn.output_tokens) == ((12000, 8) if reported else (None, None))
            attempt = turn.request_attempts[0]
            assert (attempt.input_tokens, attempt.output_tokens) == ((12000, 8) if reported else (None, None))
            observation = attempt.observation
            assert observation is not None
            assert (observation.input_tokens, observation.output_tokens, observation.thinking_tokens,
                    observation.cache_read_tokens, observation.cache_write_tokens) == (
                        (12000, 8, thinking, cached, None) if reported else (None, None, None, None, None))
            if reported:
                assert observation.raw_usage["prompt_cache_hit_tokens"] == cached
            else:
                assert "prompt_cache_hit_tokens" not in observation.raw_usage
            assert observation.field_presence["completion_tokens_details.reasoning_tokens"] is (thinking is not None)
            if thinking is not None:
                assert observation.raw_usage["completion_tokens_details.reasoning_tokens"] == thinking
            else:
                assert "completion_tokens_details.reasoning_tokens" not in observation.raw_usage
            if not reported:
                assert observation.usage_status == "unknown"

    asyncio.run(run())
