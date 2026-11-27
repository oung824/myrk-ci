"""Stop before the final record replace, after real native file/send effects."""

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

from pydantic_ai import models
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import FunctionModel, DeltaToolCall

from myrk.desktop_service import main
from tests.recovery_fixture import interrupt


async def reply(messages, _info):
    kind = os.environ["MYRK_RECOVERY_EFFECT"]
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    steps = [("create_text", {"path": "effect.txt", "content": "实际新内容"})] if kind == "create" else [
        ("read_text", {"path": "effect.txt"}),
        {"write": ("write_text", {"path": "effect.txt", "content": "实际新内容"}),
         "delete": ("delete_file", {"path": "effect.txt"}),
         "send": ("send_text", {"path": "effect.txt", "url": os.environ.get("MYRK_RECOVERY_SEND_URL", "http://127.0.0.1:1/")})}[kind],
    ]
    if len(results) < len(steps):
        name, args = steps[len(results)]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"effect-{len(results)}")}
    else:
        yield "真实影响已发生。"


@asynccontextmanager
async def factory(_key):
    yield FunctionModel(stream_function=reply, profile={"context_window": 128000, "supports_json_schema_output": True, "supports_json_object_output": True})


def stop_before_record(event, args):
    if event == "os.rename" and str(args[0]).endswith(".json.tmp"):
        record = json.loads(Path(str(args[0])).read_text(encoding="utf-8"))
        if record["turns"] and any(action["kind"] == os.environ["MYRK_RECOVERY_EFFECT"] and action["status"] == "completed" for action in record["turns"][-1]["actions"]):
            import time
            Path(os.environ["MYRK_RECOVERY_MARKER"]).write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(60)


if __name__ == "__main__":
    models.ALLOW_MODEL_REQUESTS = False
    sys.addaudithook(stop_before_record)
    main(data_directory=Path(sys.argv[1]), model_factory=factory)
