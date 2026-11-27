"""A real compacted conversation for the offline desktop integration seam."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic_ai import models
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from myrk.conversation import Conversation, ConversationRecord, Task
from myrk.desktop_service import main
from myrk.provider import ProviderConfig


async def prepare(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    history = [ModelRequest(parts=[UserPromptPart("Original goal: verify 937. Refusal remains binding; never invent evidence.")]),
               ModelResponse(parts=[ToolCallPart("read_text", {"path": "original.txt"}, "original-read")]),
               ModelRequest(parts=[ToolReturnPart("read_text", "original-tool-evidence: " + "a" * 9000 + " middle-source-refusal-937 " + "z" * 1000, "original-read")])]
    for index in range(12):
        history.extend([ModelResponse(parts=[TextPart(f"Evidence {index}: " + "historical information " * 180)]),
                        ModelRequest(parts=[UserPromptPart(f"Continue item {index}.")])])
    path = directory / "compacted.json"
    path.write_text(ConversationRecord(version=8, messages=history, turns=[]).model_dump_json(), encoding="utf-8")

    def summary(messages, info):
        return ModelResponse(parts=[TextPart("Goal: verify 937. Refusal remains binding. Failed: original file absent. Pending: verify original evidence; do not claim completion.")], usage=RequestUsage(input_tokens=12000, output_tokens=40))

    async def work(messages, info):
        yield "原工作尚待核对，拒绝和原始证据保留。"

    config = ProviderConfig(context_window_override=15000, output_token_reserve=1024, context_safety_margin=512)
    conversation = Conversation.open(path)
    result = await conversation.ask("Latest correction: read only.", document=None,
        task=Task(goal="Verify 937; preserve refusal.", scope=None, provider_config=config),
        model=FunctionModel(summary, stream_function=work, profile={"context_window": 15000}))
    assert result.status == "completed", result.error
    assert result.compactions and result.compactions[0].status == "completed"


@asynccontextmanager
async def model_factory(key):
    async def reply(messages, info):
        yield "离线继续原工作。"
    yield FunctionModel(stream_function=reply, profile={"context_window": 128000})


if __name__ == "__main__":
    models.ALLOW_MODEL_REQUESTS = False
    directory = Path(os.environ["MYRK_DATA_DIRECTORY"])
    asyncio.run(prepare(directory))
    main(data_directory=directory, model_factory=model_factory)
