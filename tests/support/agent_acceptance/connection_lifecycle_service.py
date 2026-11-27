"""Actual desktop service/SDK with a held terminal Mock stream for local lifecycle facts."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

import httpx2
from pydantic_ai import models
from pydantic_ai.models import Model

from myrk.desktop_service import serve
from myrk.provider import deepseek_model
from tests.support.agent_acceptance.replay import OfflineSession, Reply, SDKCapture
from tests.support.agent_acceptance.resume_service import ReplayClient
from tests.support.process_receipt import write_process_receipt


async def main() -> None:
    root = Path(os.environ["MYRK_CONNECTION_BASIC_ROOT"]) / os.environ["MYRK_CONNECTION_BASIC_PHASE"]
    directory = Path(os.environ["MYRK_DATA_DIRECTORY"])
    directory.mkdir(parents=True,exist_ok=True)
    session = OfflineSession("deepseek", [Reply(status=401)] if os.environ["MYRK_CONNECTION_BASIC_PHASE"] == "invalid" else [Reply(text="进展：真实 SDK 已收到本地片段。",usage=False)])
    def persist() -> None:
        attempts = [dict(task_id=turn["task"]["id"],**row) for path in directory.glob("*.json")
            for turn in json.loads(path.read_bytes()).get("turns",[]) if turn.get("task") for row in turn.get("request_attempts",[])]
        session.save(root / "exchange.json",attempts=attempts,run_id=os.environ["MYRK_CONNECTION_RUN_ID"],case_id="desktop.connection-basics",task_id="")
    class HeldStream(httpx2.AsyncByteStream):
        def __init__(self, original: httpx2.AsyncByteStream):
            self.original = original
        async def __aiter__(self) -> AsyncIterator[bytes]:
            try:
                async for chunk in self.original:
                    persist()
                    yield chunk
                    await asyncio.Event().wait()
            finally:
                session.exchanges[-1]["response_body_state"] = "cancelled_during_body"
                persist()
        async def aclose(self) -> None:
            await self.original.aclose()
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST" or session.exchanges:
            raise AssertionError("Only one predeclared native model HTTP request is allowed.")
        response = session.respond(request)
        assert isinstance(response.stream,httpx2.AsyncByteStream)
        if response.status_code == 200:
            response.stream = HeldStream(response.stream)
        persist()
        return response
    async def reject_async(_self: httpx2.AsyncHTTPTransport, _request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Real HTTP is closed for local connection qualification.")
    def reject_sync(_self: httpx2.HTTPTransport, _request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Real HTTP is closed for local connection qualification.")
    models.ALLOW_MODEL_REQUESTS = True
    setattr(httpx2.AsyncHTTPTransport,"handle_async_request",reject_async)
    setattr(httpx2.HTTPTransport,"handle_request",reject_sync)
    if receipts := os.environ.get("MYRK_PROCESS_RECEIPT_DIRECTORY"):
        write_process_receipt(Path(receipts),__file__)
    client = ReplayClient(transport=httpx2.MockTransport(respond),trust_env=False)
    @asynccontextmanager
    async def factory(key: str) -> AsyncIterator[Model]:
        async with deepseek_model(key,http_client=client) as model:
            yield model
    try:
        await serve(directory,factory,http_client=client,model_observer=lambda model:SDKCapture(model,session.sdk_inputs))
    finally:
        persist()
        await httpx2.AsyncClient.aclose(client)


if __name__ == "__main__":
    asyncio.run(main())
