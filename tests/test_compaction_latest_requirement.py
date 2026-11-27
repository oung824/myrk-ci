import asyncio
from copy import deepcopy
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.messages import CompactionPart, ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from myrk.context import CurrentContext, RequestLayout
from myrk.conversation import Task
from myrk.provider import ProviderConfig
from myrk.task_control import TaskBudget


def test_latest_user_requirement_survives_many_later_tool_roundtrips(tmp_path: Path):
    async def run():
        latest = "LATEST-USER-REQUIREMENT-937: preserve this exact correction; read only."
        source = [ModelRequest(parts=[UserPromptPart("Original goal: verify evidence.")]),
                  ModelResponse(parts=[TextPart("The initial investigation is still pending.")]),
                  ModelRequest(parts=[UserPromptPart(latest)])]
        for index in range(12):
            source.extend([ModelResponse(parts=[ToolCallPart("read_source", {}, f"call-{index}")]),
                           ModelRequest(parts=[ToolReturnPart("read_source", f"Evidence {index}: " + "Historical evidence " * 200, f"call-{index}")])])
        original = ModelMessagesTypeAdapter.dump_json(source)
        sends = []

        def respond(messages, info):
            sends.append(deepcopy(messages))
            if "compaction" in str(info.instructions).lower():
                return ModelResponse(parts=[TextPart("Goal: verify evidence. Earlier reads completed; verification remains pending.")])
            return ModelResponse(parts=[TextPart("Continue verifying the evidence.")])

        config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
        task = Task(goal="Verify evidence.", scope=None, provider_config=config)
        prepared = []
        current = CurrentContext(tmp_path / "record.json", 0, prepared, [], lambda _: None,
            config=config, budget=TaskBudget(task), attempts=[], persist=lambda: None)
        current.layout = RequestLayout(task_id=task.id, instructions="Read only.")
        await Agent(FunctionModel(respond, profile={"context_window": 15000}), instructions="Read only.", capabilities=[current]).run(message_history=source)

        final = sends[-1]
        assert len(sends) == 2 and any(isinstance(part, CompactionPart) for message in final for part in message.parts)
        assert [part.content for message in final for part in message.parts if isinstance(part, UserPromptPart)].count(latest) == 1
        calls = [part.tool_call_id for message in final for part in message.parts if isinstance(part, ToolCallPart)]
        returns = [part.tool_call_id for message in final for part in message.parts if isinstance(part, ToolReturnPart)]
        assert calls == returns and 0 < len(calls) < 12 and calls[-1] == "call-11"
        assert prepared[-1].accepted
        assert ModelMessagesTypeAdapter.dump_json(source) == original

    asyncio.run(run())
