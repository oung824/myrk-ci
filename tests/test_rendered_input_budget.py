"""Budget behavior through the public SDK path; all provider traffic is mocked."""

import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from pydantic import BaseModel, Field
from pydantic_ai import Agent, NativeOutput, models
from pydantic_ai.capabilities import Hooks
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_core import to_json

from myrk.compaction import ContextLimitExceeded, measure_request
from myrk.context import CurrentContext, RequestLayout
from myrk.provider import ProviderConfig, configured_model


async def exercise_append_state(path: Path, wire: list[bytes], *, window: int, state_size: int = 400):
    prepared = []
    state = "Recorded state 0: " + "甲" * state_size
    config = ProviderConfig(auto_compaction=False, context_window_override=window,
                            output_token_reserve=128, context_safety_margin=512)
    current = CurrentContext(path, 0, prepared, [], lambda _: None, config=config)
    current.layout = RequestLayout(task_id="accounting-probe", instructions="Read the local state once, then report.")

    def respond(request):
        wire.append(request.content)
        message = {"role": "assistant", "content": "Recorded."}
        if len(wire) == 1:
            message = {"role": "assistant", "content": None,
                       "tool_calls": [{"id": "state-read", "type": "function",
                                       "function": {"name": "read_local_state", "arguments": "{}"}}]}
        return httpx2.Response(200, json={"id": "synthetic", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if len(wire) == 1 else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        async with configured_model("synthetic-only", config, http_client=http) as model:
            agent = Agent(model, instructions=current.layout.instructions, capabilities=[current])

            @agent.instructions(name="execution-state", on_change="append")
            def execution_state():
                return state

            @agent.tool_plain
            def read_local_state() -> str:
                """Read a fixed local state, without files or network."""
                nonlocal state
                state = "Recorded state 1: " + "乙" * state_size
                return "State was recorded."

            return await agent.run("Read, then report."), prepared, wire


@pytest.mark.parametrize("state_size", [379, 380, 400])
def test_rendered_state_cannot_escape_the_input_budget(tmp_path: Path, monkeypatch, state_size):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)

    async def run():
        wire = []
        with pytest.raises(ContextLimitExceeded, match="资料容量不足"):
            await exercise_append_state(tmp_path / "record.json", wire, window=1381, state_size=state_size)
        assert len(wire) < 2

    asyncio.run(run())


def test_wide_budget_keeps_one_sdk_rendering_of_each_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)

    async def run():
        wire = []
        result, prepared, _ = await exercise_append_state(tmp_path / "record.json", wire, window=100000)
        assert result.output == "Recorded." and len(wire) == len(prepared) == 2
        contents = ["\n".join(message.get("content") or "" for message in json.loads(body)["messages"]) for body in wire]
        assert contents[0].count("Recorded state 0:") == 1
        assert "Recorded state 1:" not in contents[0]
        assert contents[1].count("Recorded state 0:") == contents[1].count("Recorded state 1:") == 1
        assert all(item.measurement_source == "sdk_rendered_local_text_estimate" for item in prepared)

    asyncio.run(run())


def test_measurement_uses_effective_model_defaults_without_changing_the_request():
    async def run():
        measurements, observed_settings = [], []
        config = ProviderConfig(context_window_override=9000, output_token_reserve=128, context_safety_margin=512)

        async def inspect(_ctx, request):
            original = to_json([request.messages, request.model_settings, request.model_request_parameters])
            first = measure_request(request, request.messages, config)
            assert measure_request(request, request.messages, config) == first
            assert to_json([request.messages, request.model_settings, request.model_request_parameters]) == original
            measurements.append(first)
            return request

        def respond(_messages, info):
            observed_settings.append(info.model_settings)
            return ModelResponse(parts=[TextPart("Recorded.")])

        for name in ["measurement-one", "measurement-two"]:
            model = FunctionModel(respond, model_name=name, settings={"max_tokens": 700},
                                  profile={"context_window": 9000})
            result = await Agent(model, instructions="Read only.", capabilities=[Hooks(prepare_model_request=inspect)]).run(
                "Report.", model_settings={"temperature": 0.25})
            assert result.output == "Recorded."
        assert all(item.budget == 7788 for item in measurements)
        assert all(options["max_tokens"] == 700 and options["temperature"] == 0.25 for options in observed_settings)
        assert measurements[0].key != measurements[1].key

    asyncio.run(run())


def test_native_output_schema_is_part_of_the_measured_request(tmp_path: Path):
    class Receipt(BaseModel):
        receipt: int

    class DescribedReceipt(BaseModel):
        receipt: int = Field(description="A receipt number from the fixed material. " * 300)

    async def run():
        prepared, schemas = [], []
        config = ProviderConfig(auto_compaction=False, context_window_override=100000,
                                output_token_reserve=128, context_safety_margin=512)

        def respond(_messages, info):
            assert info.model_request_parameters.output_mode == "native"
            schemas.append(info.model_request_parameters.output_object.json_schema)
            return ModelResponse(parts=[TextPart('{"receipt":7}')])

        model = FunctionModel(respond, model_name="native-receipt",
                              profile={"context_window": 100000, "supports_json_schema_output": True})
        for schema in [Receipt, DescribedReceipt]:
            current = CurrentContext(tmp_path / "record.json", 0, prepared, [], lambda _: None, config=config)
            current.layout = RequestLayout(task_id="native-receipt", instructions="Report the receipt.")
            result = await Agent(model, instructions="Report the receipt.", output_type=NativeOutput(schema),
                                 capabilities=[current]).run("Report.")
            assert result.output.receipt == 7
        assert "description" not in schemas[0]["properties"]["receipt"]
        assert len(schemas[1]["properties"]["receipt"]["description"]) > 1000
        assert prepared[1].input_tokens > prepared[0].input_tokens + 100
        assert prepared[1].request_key != prepared[0].request_key

    asyncio.run(run())
