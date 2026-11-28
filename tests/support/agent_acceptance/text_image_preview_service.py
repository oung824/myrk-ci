"""Real service/native SDK; only final model HTTP replies are synthetic."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from typing import Any

import httpx2
from pydantic_ai import models
from pydantic_ai.models import Model
from myrk.desktop_service import serve
from myrk.observed_http import ObservedHTTPTransport
from myrk.provider import deepseek_model
from tests.support.agent_acceptance.replay import OfflineSession, Reply, SDKCapture
from tests.support.agent_acceptance.resume_service import PersistedStream, ReplayClient
from tests.support.process_receipt import write_process_receipt


async def main() -> None:
    root = Path(os.environ["MYRK_PREVIEW_ROOT"])
    phase = os.environ["MYRK_ACCEPTANCE_RESTART_PHASE"]
    directory = Path(os.environ["MYRK_DATA_DIRECTORY"])
    directory.mkdir(parents=True,exist_ok=True)
    def tool_results(payload: dict[str, Any]) -> list[str]:
        return [message["content"] for message in payload["messages"] if message["role"] == "tool"]
    def command(payload: dict[str, Any]) -> dict[str, Any]:
        definition = json.loads(tool_results(payload)[-1])
        chat = "B" if "SETUP-CHAT-B" in json.dumps(payload,ensure_ascii=False) else "A"
        expected = root / "expected"
        source = "other.txt" if chat == "B" else "after.txt"
        code = f"from pathlib import Path; import shutil; e=Path({str(expected)!r}); shutil.copyfile(e/{source!r},'report.txt'); shutil.copyfile(e/'long.txt','long.txt'); shutil.copyfile(e/'picture.png','picture.png')"
        return {"name":"run_command_with_outputs","schema_version":definition["schema_version"],"arguments":{"program":sys.executable,"arguments":["-B","-c",code],"output_paths":["report.txt","long.txt","picture.png"]}}
    def deliver(payload: dict[str, Any]) -> dict[str, Any]:
        results = tool_results(payload)
        definition = json.loads(results[-1])
        changes = json.loads(results[-2].split("文件改动证据：",1)[1])
        return {"name":"deliver_files","schema_version":definition["schema_version"],"arguments":{"change_ids":[change["id"] for change in changes]}}
    setup = [Reply(tool="describe_extension",arguments={"name":"run_command_with_outputs"}),Reply(tool="call_extension",arguments=command),Reply(tool="describe_extension",arguments={"name":"deliver_files"}),Reply(tool="call_extension",arguments=deliver),Reply(text="实际文件已明确交付，等待用户检查。")]
    session = OfflineSession("deepseek",setup*2 if phase == "work" else [])
    def persist() -> None:
        attempts = [dict(task_id=turn["task"]["id"],**row) for path in directory.glob("*.json") for turn in json.loads(path.read_bytes()).get("turns",[]) if turn.get("task") for row in turn.get("request_attempts",[])]
        session.save(root / phase / "exchange.json",attempts=attempts,run_id=os.environ["MYRK_PREVIEW_RUN_ID"],case_id="preview.text-diff-image",task_id="")
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST":
            raise AssertionError("No catalogue, connection or other network requests are allowed.")
        response=session.respond(request)
        assert isinstance(response.stream,httpx2.AsyncByteStream)
        response.stream=PersistedStream(response.stream,persist)
        persist()
        return response
    async def reject_async(_self: httpx2.AsyncHTTPTransport,_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Real network is closed.")
    def reject_sync(_self: httpx2.HTTPTransport,_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Real network is closed.")
    models.ALLOW_MODEL_REQUESTS=True
    setattr(httpx2.AsyncHTTPTransport,"handle_async_request",reject_async)
    setattr(httpx2.HTTPTransport,"handle_request",reject_sync)
    if receipt := os.environ.get("MYRK_PROCESS_RECEIPT_DIRECTORY"):
        write_process_receipt(Path(receipt),__file__)
    client=ReplayClient(transport=ObservedHTTPTransport(httpx2.MockTransport(respond)),trust_env=False)
    @asynccontextmanager
    async def factory(key: str) -> AsyncIterator[Model]:
        async with deepseek_model(key,http_client=client) as model:
            yield model
    persist()
    try:
        await serve(directory,factory,http_client=client,model_observer=lambda model:SDKCapture(model,session.sdk_inputs))
    finally:
        persist()
        await httpx2.AsyncClient.aclose(client)


if __name__ == "__main__":
    asyncio.run(main())
