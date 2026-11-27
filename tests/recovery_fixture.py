"""A real service with an OS-file-replace interruption boundary, no production hooks."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import time

from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from myrk.desktop_service import main


async def reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    steps = [("read_text", {"path": "notes.txt"}),
             ("edit_text", {"path": "notes.txt", "old_text": "苹果 3", "new_text": "苹果 5"}),
             ("run_check", {"name": "指定检查"})]
    if os.environ.get("MYRK_RECOVERY_GENERATED") == "1":
        steps[-1] = ("run_command", {"program": sys.executable, "arguments": ["check.py"]})
    if len(results) < len(steps):
        name, args = steps[len(results)]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"recovery-{len(results)}")}
    else:
        yield "真实成果已核对。"
        if os.environ.get("MYRK_RECOVERY_POINT") == "after_result":
            await asyncio.sleep(30)


@asynccontextmanager
async def factory(_key: str) -> AsyncIterator[Model]:
    yield FunctionModel(stream_function=reply, profile={"context_window": 128000, "supports_json_schema_output": True, "supports_json_object_output": True})


def interrupt(event: str, args: tuple[object, ...]) -> None:
    if event != "os.rename" or not str(args[0]).endswith(".json.tmp"):
        return
    pending = Path(str(args[0]))
    record = json.loads(pending.read_text(encoding="utf-8"))
    actions = record["turns"][-1]["actions"] if record["turns"] else []
    point = os.environ.get("MYRK_RECOVERY_POINT")
    if point in {"after_edit", "after_check", "after_command"} and any(action["kind"] == {"after_edit": "edit", "after_check": "check", "after_command": "command"}[point] and action["status"] == "completed" for action in actions):
        Path(os.environ["MYRK_RECOVERY_MARKER"]).write_text(str(os.getpid()), encoding="utf-8")
        crash_flag = os.environ.get("MYRK_RECOVERY_CRASH_FLAG")
        if crash_flag:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if Path(crash_flag).exists():
                    os._exit(71)
                if Path(os.environ["MYRK_TEST_NORMAL_EXIT_FLAG"]).exists():
                    break
                time.sleep(0.025)
        else:
            time.sleep(60)


if __name__ == "__main__":
    models.ALLOW_MODEL_REQUESTS = False
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.environ["MYRK_DATA_DIRECTORY"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "service.pid").write_text(str(os.getpid()), encoding="utf-8")
    if receipts := os.environ.get("MYRK_PROCESS_RECEIPT_DIRECTORY"):
        from tests.support.process_receipt import write_process_receipt
        write_process_receipt(Path(receipts), __file__)
    sys.addaudithook(interrupt)
    main(data_directory=directory, model_factory=factory)
