"""Public JSON command channel with a held Windows process identity and exit."""

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from types import TracebackType
from typing import Any

from pydantic import TypeAdapter

from tests.support.agent_acceptance.core import CaseContext
from tests.support.agent_acceptance.formal import environment, provider_config
from tests.support.agent_acceptance.replay import Reply
from tests.support.process_observation import Observation, ProcessObserver, SpawnEvidence


class ServiceChannel:
    def __init__(self, context: CaseContext, name: str, replies: list[Reply], *, gates: list[int] | None = None,
                 records: Path | None = None, interrupt_after_edit: bool = False):
        self.context, self.root = context, context.root / name
        if self.root.exists() and (self.root.is_symlink() or self.root.is_junction() or any(self.root.iterdir())):
            raise ValueError("A service channel requires its exclusive new or predeclared empty control directory.")
        self.root.mkdir(exist_ok=True)
        self.records = records or context.root / "records"
        self.script = self.root / "script.json"
        self.script.write_text(json.dumps({"protocol": context.protocol, "run_id": context.run_id, "case_id": context.case_id,
            "replies": [asdict(reply) for reply in replies], "gates": gates or [], "interrupt_after_edit": interrupt_after_edit}), encoding="utf-8")
        self.events: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.stdout = bytearray()
        self.observer = ProcessObserver()

    async def __aenter__(self) -> "ServiceChannel":
        argv = [sys.executable, "-B", "-m", "tests.support.agent_acceptance.resume_service"]
        self.process = await asyncio.create_subprocess_exec(*argv, cwd=Path(__file__).resolve().parents[3],
            env={**environment(self.root / "environment"), "MYRK_DATA_DIRECTORY": str(self.records),
                 "MYRK_ACCEPTANCE_RESUME_SCRIPT": str(self.script), "PYTHONIOENCODING": "utf-8"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
        initial = self.observer.capture(self.process.pid)
        assert initial.identity is not None
        initial = self.observer.capture(self.process.pid, direct_spawn=SpawnEvidence(initial.identity, os.getpid(), tuple(argv),
            str(Path(__file__).resolve().parents[3]), "caller_spawn"))
        assert initial.ownership == "owned" and initial.state == "running"
        self.startup = initial

        async def read() -> None:
            assert self.process.stdout is not None
            try:
                async for line in self.process.stdout:
                    self.stdout.extend(line)
                    event = json.loads(line)
                    self.events.append(event)
                    self.queue.put_nowait(event)
            finally:
                await self.process.wait()
                self.queue.put_nowait({"event": "__channel_closed__", "exit_code": self.process.returncode})

        self.reader = asyncio.create_task(read())
        assert self.process.stderr is not None
        self.stderr = asyncio.create_task(self.process.stderr.read())
        try:
            await self.receive("ready")
        except BaseException:
            await self.__aexit__(*sys.exc_info())
            raise
        return self

    async def send(self, command: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(command) + "\n").encode())
        await self.process.stdin.drain()

    async def receive(self, event: str, **fields: Any) -> dict[str, Any]:
        async with asyncio.timeout(30):
            while True:
                value = await self.queue.get()
                if value["event"] == "__channel_closed__":
                    raise RuntimeError(f"Service closed before {event}; actual exit code {value['exit_code']}; originals: {self.root}")
                if value["event"] == event and all(value.get(key) == expected for key, expected in fields.items()):
                    return value

    async def configure(self) -> None:
        config = provider_config(self.context.protocol).model_copy(update={"input_token_limit_override": 100000})
        await self.send({"action": "configure", "key": "synthetic-resume-only", "provider_config": config.model_dump(mode="json")})
        await self.receive("configured")

    async def arrived(self, index: int) -> None:
        async with asyncio.timeout(30):
            while not (self.root / "transport" / f"arrived-{index}").is_file():
                await asyncio.sleep(.01)

    def release(self, index: int) -> None:
        (self.root / "transport" / f"release-{index}").write_text("release planned HTTP response\n", encoding="utf-8")

    async def __aexit__(self, _kind: type[BaseException] | None, _error: BaseException | None, _traceback: TracebackType | None) -> None:
        try:
            if self.process.returncode is None:
                await self.send({"action": "shutdown"})
            await asyncio.wait_for(self.process.wait(), 15)
            await asyncio.gather(self.reader, return_exceptions=True)
            (self.root / "stdout.txt").write_bytes(bytes(self.stdout))
            (self.root / "stderr.txt").write_bytes(await self.stderr)
        finally:
            statuses = self.observer.status()
            (self.root / "process.json").write_text(json.dumps({"startup": TypeAdapter(Observation).dump_python(self.startup, mode="json"),
                "after": [TypeAdapter(Observation).dump_python(item, mode="json") for item in statuses],
                "returncode": self.process.returncode,
                "reader_error": str(self.reader.exception()) if self.reader.done() and not self.reader.cancelled() and self.reader.exception() is not None else None}, indent=2), encoding="utf-8")
            (self.root / "events.json").write_text(json.dumps(self.events, ensure_ascii=False, indent=2), encoding="utf-8")
            self.observer.close()
        if _kind is None and self.reader.done() and not self.reader.cancelled() and self.reader.exception() is not None:
            raise RuntimeError("The actual service JSON reader failed; see preserved process.json and stdout.") from self.reader.exception()

    def evidence(self) -> dict[str, Path]:
        return {f"{self.root.name}.{name}": self.root / filename for name, filename in (
            ("script", "script.json"), ("exchange", "exchange.json"), ("events", "events.json"), ("process", "process.json"),
            ("stdout", "stdout.txt"), ("stderr", "stderr.txt"))}
