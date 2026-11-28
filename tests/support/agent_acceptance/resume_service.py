"""Real service and native SDKs with a finite, externally gated HTTP replay."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from hashlib import sha256
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
from myrk.request_observation import current_request_attempt
from tests.support.agent_acceptance.replay import OfflineSession, Reply, SDKCapture


class PersistedStream(httpx2.AsyncByteStream):
    def __init__(self, original: httpx2.AsyncByteStream, persist: Callable[[], None]):
        self.original, self.persist = original, persist

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.original:
                self.persist()
                yield chunk
        finally:
            self.persist()

    async def aclose(self) -> None:
        await self.original.aclose()


class ReplayClient(httpx2.AsyncClient):
    """The fixture owns its shared terminal transport across SDK connections."""

    async def aclose(self) -> None:
        pass


async def main() -> None:
    script_path = Path(os.environ["MYRK_ACCEPTANCE_RESUME_SCRIPT"])
    script = json.loads(script_path.read_text(encoding="utf-8"))
    root = script_path.parent
    transport = root / "transport"
    transport.mkdir(exist_ok=True)
    session = OfflineSession(script["protocol"], [Reply(**item) for item in script["replies"]])
    directory = Path(os.environ["MYRK_DATA_DIRECTORY"])
    directory.mkdir(parents=True, exist_ok=True)
    pending_observations: list[dict[str, Any]] = []

    def record_attempts(record: dict[str, Any]) -> list[dict[str, Any]]:
        captured: list[dict[str, Any]] = []
        for turn in record.get("turns", []):
            task_id = turn["task"]["id"] if turn.get("task") else ""
            captured.extend({"task_id": task_id, **attempt} for attempt in turn.get("request_attempts", []))
            for action in turn.get("actions", []):
                if action.get("review"):
                    captured.extend({"task_id": task_id, **attempt} for attempt in action["review"].get("request_attempts", []))
        return captured

    def persist() -> None:
        attempts = {}
        for path in directory.rglob("*.json"):
            if path.parent != directory and not path.parent.name.endswith("-delegations"):
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            for attempt in record_attempts(record):
                attempts[tuple(attempt.get(name) for name in ("task_id", "number", "actor", "purpose", "delegation_id"))] = attempt
        saved = session.save(root / "exchange.json", attempts=list(attempts.values()), run_id=script["run_id"], case_id=script["case_id"], task_id="")
        if pending_observations:
            observation = json.loads(saved.read_bytes())
            observation["pending_observations"] = pending_observations
            saved.write_text(json.dumps(observation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    persist()

    async def respond(request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST":
            raise AssertionError("Catalogue and other unplanned HTTP are closed.")
        index = len(session.exchanges)
        if index >= script.get("max_http_attempts", 100):
            raise AssertionError("The finite desktop replay HTTP limit was reached.")
        attempt = current_request_attempt()
        if script.get("compaction_reply") is not None and attempt is not None and attempt.purpose == "compaction":
            session.replies.insert(index, Reply(**script["compaction_reply"]))
        response = session.respond(request)
        persist()
        (transport / f"arrived-{index}").write_text(str(os.getpid()), encoding="utf-8")
        if index in script.get("gates", []):
            try:
                async with asyncio.timeout(30):
                    while not (transport / f"release-{index}").is_file():
                        await asyncio.sleep(.01)
            except asyncio.CancelledError:
                session.exchanges[index]["response_body_state"] = "cancelled_before_delivery"
                persist()
                raise
        assert isinstance(response.stream, httpx2.AsyncByteStream)
        response.stream = PersistedStream(response.stream, persist)
        return response

    async def reject_async(_self: httpx2.AsyncHTTPTransport, _request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Only the injected MockTransport may send requests.")

    def reject_sync(_self: httpx2.HTTPTransport, _request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Only the injected MockTransport may send requests.")

    models.ALLOW_MODEL_REQUESTS = True
    setattr(httpx2.AsyncHTTPTransport, "handle_async_request", reject_async)
    setattr(httpx2.HTTPTransport, "handle_request", reject_sync)
    if os.environ.get("MYRK_PROCESS_RECEIPT_DIRECTORY"):
        from tests.support.process_receipt import write_process_receipt
        write_process_receipt(Path(os.environ["MYRK_PROCESS_RECEIPT_DIRECTORY"]), __file__)

    def interrupt(event: str, args: tuple[object, ...]) -> None:
        if not script.get("interrupt_after_edit") or event != "os.rename" or not str(args[0]).endswith(".json.tmp"):
            return
        pending = Path(str(args[0]))
        if not pending.resolve().is_relative_to(directory.resolve()):
            return
        original = pending.read_bytes()
        record = json.loads(original)
        if record.get("turns") and any(action["kind"] == "edit" and action["status"] == "completed" for action in record["turns"][-1]["actions"]):
            grouped: dict[str, list[dict[str, Any]]] = {}
            for attempt in record_attempts(record):
                grouped.setdefault(attempt["task_id"], []).append(attempt)
            pending_observations.extend({"source_path": str(pending), "source_sha256": sha256(original).hexdigest(),
                "task_id": task_id, "attempts": attempts, "committed": False,
                "source_kind": "uncommitted_pending_record"} for task_id, attempts in grouped.items())
            persist()
            (root / "planned-local-exit.json").write_text(json.dumps({"pid": os.getpid(), "exit_code": 71,
                "boundary": "before replacing saved record after actual edit", "pending": str(pending)}), encoding="utf-8")
            os._exit(71)

    sys.addaudithook(interrupt)
    client = ReplayClient(transport=ObservedHTTPTransport(httpx2.MockTransport(respond)), trust_env=False)

    @asynccontextmanager
    async def factory(key: str) -> AsyncIterator[Model]:
        async with deepseek_model(key, http_client=client) as model:
            yield model

    try:
        await serve(directory, factory, http_client=client,
                    model_observer=lambda model: SDKCapture(model, session.sdk_inputs))
    finally:
        persist()
        await httpx2.AsyncClient.aclose(client)


if __name__ == "__main__":
    asyncio.run(main())
