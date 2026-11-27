"""Resume a saved conversation in another process with a transport-only error."""

import asyncio
import json
from pathlib import Path
import sys

from pydantic_ai import models
from pydantic_ai.exceptions import ModelContextWindowExceededError
from pydantic_ai.models.function import FunctionModel

from myrk.conversation import Conversation


async def resume(path: Path) -> None:
    conversation = Conversation.open(path)
    task = conversation.turns[-1].task
    assert task is not None
    assert (await conversation.verify_recovery_async(task.id)).can_resume

    async def respond(messages, info):
        raise ModelContextWindowExceededError("synthetic", "planned cross-process capacity error")
        yield "unreachable"

    def summarize(messages, info):
        raise AssertionError("A used recovery must not dispatch another summary.")

    turn = await conversation.resume_task(task.id, model=FunctionModel(
        summarize, stream_function=respond, profile={"context_window": 30000}))
    task = turn.task
    assert task is not None and task.work_step is not None
    print(json.dumps({"task_id": task.id, "step_id": task.work_step.id, "requests": task.total_requests,
                      "turns": task.turns_used, "status": turn.status, "decision": turn.request_attempts[-1].decision}))


if __name__ == "__main__":
    models.ALLOW_MODEL_REQUESTS = False
    asyncio.run(resume(Path(sys.argv[1])))
