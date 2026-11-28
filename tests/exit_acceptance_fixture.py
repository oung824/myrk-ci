"""Deterministic model boundary for real desktop exit acceptance."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from tests.support.process_receipt import write_process_receipt


async def reply(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    directory = Path(os.environ["MYRK_EXIT_CASE_DIRECTORY"])
    with (directory / "requests.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"pid": os.getpid(), "step": "request"}) + "\n")
    await asyncio.sleep(0.4)
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    steps = [("read_text", {"path": "notes.txt"}),
             ("edit_text", {"path": "notes.txt", "old_text": "苹果 3", "new_text": "苹果 5"}),
             ("delete_file", {"path": ".agents/keep.txt"}) if os.environ["MYRK_EXIT_MODE"] == "approval" else ("run_check", {"name": "指定检查"})]
    if len(results) < len(steps):
        name, arguments = steps[len(results)]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f"exit-{len(results)}")}
    else:
        yield "真实检查结果已保留：" + str(results[-1].content)


@asynccontextmanager
async def factory(_key: str) -> AsyncIterator[Model]:
    yield FunctionModel(stream_function=reply, profile={"context_window": 128000, "supports_json_schema_output": True, "supports_json_object_output": True})


if __name__ == "__main__":
    models.ALLOW_MODEL_REQUESTS = False
    role = sys.argv[1] if len(sys.argv) > 1 else "service"
    if role == "service":
        from myrk.desktop_service import main
    if role == "check":
        os.environ.update(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
    directory = Path(os.environ["MYRK_EXIT_CASE_DIRECTORY"])
    receipt = write_process_receipt(Path(os.environ["MYRK_PROCESS_RECEIPT_DIRECTORY"]), __file__)
    if receipt is None:
        raise SystemExit(0)
    with (directory / "roles.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"role": role, "pid": os.getpid(), "parent_pid": os.getppid(),
            "receipt": str(receipt), "at": time.time(), "argv": [sys.executable, *sys.orig_argv[1:]]}) + "\n")
    stop = Path(os.environ["MYRK_TEST_NORMAL_EXIT_FLAG"])
    if role == "check":
        assert (directory / "notes.txt").read_text(encoding="utf-8") == "苹果 5\n"
        runs = directory / "runs.txt"
        runs.write_text((runs.read_text(encoding="utf-8") if runs.exists() else "") + "x", encoding="utf-8")
        print("real-exit-check-stdout", flush=True)
        print("real-exit-check-stderr", file=sys.stderr, flush=True)
        child = subprocess.Popen([sys.executable, "-u", "-m", "tests.exit_acceptance_fixture", "child"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        while not stop.exists():
            time.sleep(0.05)
        child.wait(timeout=10)
    elif role == "child":
        while not stop.exists():
            with (directory / "heartbeat.txt").open("a", encoding="utf-8") as stream:
                stream.write(f"{os.getpid()} {time.monotonic()}\n")
            time.sleep(0.05)
    elif role == "service":
        from tests.support.observed_check_runner import install_check_observation
        install_check_observation()
        main(data_directory=Path(os.environ["MYRK_DATA_DIRECTORY"]), model_factory=factory)
    else:
        raise ValueError(f"Unknown controlled role: {role}")
