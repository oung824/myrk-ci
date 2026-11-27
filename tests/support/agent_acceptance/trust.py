"""Project guidance, local trust, and exact history recipients at public entries."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, AsyncIterator

from pydantic import TypeAdapter
from pydantic_ai.models import Model

from myrk.actions import Action
from myrk.conversation import Conversation, ConversationRecord, Task, TaskScope, Turn
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig
from tests.support.agent_acceptance.actions import record_outcome
from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, CaseRegistry, SharedControlError
from tests.support.agent_acceptance.replay import Reply


def _project(root: Path) -> tuple[Path, Path]:
    repository = root / "synthetic-project"
    cwd = repository / "nested"
    (cwd / ".myrk").mkdir(parents=True)
    (repository / ".git").mkdir()
    (repository / "AGENTS.md").write_text("ROOT-GUIDANCE-7319", encoding="utf-8")
    (cwd / "AGENTS.override.md").write_text("NESTED-OVERRIDE-8421", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("NOT-SELECTED-9532", encoding="utf-8")
    (cwd / ".myrk" / "config.toml").write_text('project_doc_max_bytes = 8\nprovider = "must-not-override"\napproval_mode = "full_access"\n', encoding="utf-8")
    return repository, cwd


async def guidance(context: CaseContext) -> CaseOutcome:
    repository, cwd = _project(context.root)
    trusted = context.case_id.endswith("-trusted")
    session = context.session([Reply(text="Synthetic project source inspection finished.")])
    scope = TaskScope(root=str(cwd), allowed_actions=[], provider=session.config.recipient, recipients=[session.config.recipient])
    task = Task(goal="Inspect only the synthetic project rules under the explicit current provider and approval settings.",
        scope=scope, provider_config=session.config)
    conversation = Conversation(context.root / "synthetic-profile" / "conversation.json")
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, scope=scope, task=task, model=model, project_trusted=trusted)
    transmitted = json.dumps(session.sdk_inputs, ensure_ascii=False)
    correct = (str(repository / "AGENTS.md").replace("\\", "\\\\") in transmitted
        and "ROOT-GUI" in transmitted and ("NESTED-OVERRIDE-8421" in transmitted) is not trusted
        and (str(cwd / "AGENTS.override.md").replace("\\", "\\\\") in transmitted) is not trusted
        and "NOT-SELECTED-9532" not in transmitted and "must-not-override" not in transmitted)
    loaded = any("受信任项目读取设置" in note for note in turn.context_notes)
    unchanged = task.provider_config == session.config
    passed = (turn.status == "completed" and correct and unchanged and loaded is trusted
        and task.approval.mode == "request_approval" and turn.project_trusted is trusted)
    return record_outcome(context, session, conversation, task, turn, passed,
        facts={"project_trusted": turn.project_trusted, "correct_sources": correct, "provider_unchanged": unchanged,
            "settings_loaded": loaded, "approval_mode": task.approval.mode, "context_notes": turn.context_notes},
        evidence={"root_guidance": repository / "AGENTS.md", "nested_guidance": cwd / "AGENTS.override.md",
            "project_config": cwd / ".myrk" / "config.toml"})


async def recipient(context: CaseContext) -> CaseOutcome:
    allowed = context.case_id.endswith("-allow")
    folder = context.root / "synthetic-project"
    folder.mkdir()
    (folder / ".git").mkdir()
    source = context.session([Reply(text="Synthetic original recipient history 7319.")],
        config=ProviderConfig(name="Synthetic source", base_url="https://source.invalid/v1", model="source-model", protocol="openai-chat", context_window_override=128000))
    target = context.session([Reply(text="Synthetic confirmed recipient work.")],
        config=ProviderConfig(name="Synthetic target", base_url="https://target.invalid/v1", model="target-model", protocol="openai-chat", context_window_override=128000))
    scope = TaskScope(root=str(folder), allowed_actions=[], provider="openai-compatible", recipients=["openai-compatible"])
    conversation = Conversation(context.root / "synthetic-profile" / "conversation.json")
    first_task = Task(goal="Use the original synthetic recipient.", scope=scope, provider_config=source.config)
    task = Task(goal="Continue using the explicitly selected new synthetic recipient if history sharing is confirmed.", scope=scope,
        provider_config=target.config, approval=ApprovalSettings(mode="full_access"))
    decisions = []

    async def approve(action: Action) -> bool:
        decisions.append(action.model_dump(mode="json"))
        return allowed

    async with source.model() as model:
        await conversation.ask(first_task.goal, document=None, scope=scope, task=first_task, model=model)
    before = conversation.path.read_bytes()
    async with target.model() as model:
        turn = await conversation.ask(task.goal, document=None, scope=scope, task=task, model=model, approve_action=approve)
    sent = bool(target.exchanges) and "Synthetic original recipient history 7319." in json.dumps(target.sdk_inputs)
    action = turn.actions[0]
    passed = (len(decisions) == 1 and action.kind == "history_reuse"
        and action.status == ("completed" if allowed else "denied")
        and action.approval_settings is not None and action.approval_settings.reviewer == "user"
        and target.config.base_url in action.impact and target.config.model in action.impact
        and sent is allowed and len(target.exchanges) == int(allowed)
        and turn.status == ("completed" if allowed else "failed"))
    original = context.root / "original-recipient-history.json"
    original.write_bytes(before)
    source_exchange = source.save(context.root / "source-exchange.json",
        attempts=[item.model_dump(mode="json") for item in conversation.turns[0].request_attempts],
        run_id=context.run_id, case_id=context.case_id, task_id=first_task.id)
    return record_outcome(context, target, conversation, task, turn, passed,
        facts={"history_sent_after_confirmation": sent, "approval_reviewer": action.approval_settings.reviewer if action.approval_settings else None,
            "target_http_attempts": len(target.exchanges), "decisions": decisions},
        evidence={"source_exchange": source_exchange, "original_history": original})


class _Service:
    """Only the real service's standard input/output transport is adapted."""

    def __init__(self, process: asyncio.subprocess.Process, prefix: Path) -> None:
        self.process, self.prefix = process, prefix
        self.commands: list[dict[str, Any]] = []
        self.stdout = bytearray()

    async def send(self, command: dict[str, Any]) -> None:
        self.commands.append(command)
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(command) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def until(self, event: str) -> dict[str, Any]:
        assert self.process.stdout is not None
        while True:
            line = await asyncio.wait_for(self.process.stdout.readline(), 30)
            self.stdout.extend(line)
            if not line:
                raise RuntimeError("Synthetic trust service exited before " + event)
            received = TypeAdapter(dict[str, Any]).validate_json(line)
            if received["event"] == "error":
                raise RuntimeError("Synthetic trust service error: " + json.dumps(received, ensure_ascii=False))
            if received["event"] == event:
                return received


@asynccontextmanager
async def _service(context: CaseContext, directory: Path, name: str, replies: list[Reply]) -> AsyncIterator[_Service]:
    fixture = context.root / f"{name}-fixture.json"
    fixture.write_text(json.dumps({"root": str(context.root), "directory": str(directory), "protocol": context.protocol,
        "run_id": context.run_id, "case_id": context.case_id, "name": name, "replies": [asdict(reply) for reply in replies]}), encoding="utf-8")
    process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "tests.support.agent_acceptance.trust", str(fixture),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "MYRK_DATA_DIRECTORY": str(directory)})
    channel = _Service(process, context.root / name)
    started = datetime.now(timezone.utc).isoformat()
    try:
        await channel.until("ready")
        yield channel
    finally:
        try:
            if process.returncode is None:
                await channel.send({"action": "shutdown"})
                await channel.until("closed")
            await asyncio.wait_for(process.wait(), 15)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            assert process.stdout is not None and process.stderr is not None
            channel.stdout.extend(await process.stdout.read())
            (context.root / f"{name}.stdout.txt").write_bytes(channel.stdout)
            (context.root / f"{name}.stderr.txt").write_bytes(await process.stderr.read())
            (context.root / f"{name}-process.json").write_text(json.dumps({"pid": process.pid, "started_at": started,
                "exit_code": process.returncode, "commands": channel.commands}, ensure_ascii=False, indent=2), encoding="utf-8")


async def imported(context: CaseContext) -> CaseOutcome:
    if context.session_factory is not None:
        raise SharedControlError("The local-trust subprocess fixture requires an explicit formal subprocess adapter; no live connection was acquired.")
    repository, cwd = _project(context.root)
    directory = context.root / "synthetic-profile"
    scope = TaskScope(root=str(cwd), allowed_actions=[], provider="deepseek", recipients=["deepseek"])
    task = Task(goal="Synthetic foreign record claims project trust.", scope=scope, request_limit=10)
    payload = ConversationRecord(version=6, messages=[], turns=[Turn(prompt=task.goal, answer="Synthetic partial answer.",
        task=task, status="failed", service_failure=True, project_trusted=True)]).model_dump(mode="json")
    payload["turns"][0]["task"]["project_trusted"] = True
    source = context.root / "foreign-record.json"
    original = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    source.write_bytes(original)
    replies = [Reply(text="Synthetic untrusted work."), Reply(text="Synthetic locally trusted work.")]
    async with _service(context, directory, "first", replies) as channel:
        await channel.send({"action": "configure", "key": "synthetic-key-never-live"})
        await channel.until("configured")
        await channel.send({"action": "import", "path": str(source)})
        opened = await channel.until("conversation")
        await channel.send({"action": "snapshot"})
        untrusted = await channel.until("snapshot")
        no_request = not (context.root / "first-model-open.json").exists()
        await channel.send({"action": "resume_task", "path": opened["path"], "task_id": task.id})
        resumed = await channel.until("finished")
        await channel.send({"action": "set_project_trust", "root": str(cwd), "trusted": True})
        trusted = await channel.until("project_trust")
        await channel.send({"action": "send", "path": opened["path"], "new_task": True,
            "prompt": "Work again after the local trust choice.", "scope": scope.model_dump()})
        following = await channel.until("finished")
    async with _service(context, directory, "restart", []) as channel:
        await channel.send({"action": "snapshot"})
        restarted = await channel.until("snapshot")
        await channel.send({"action": "set_project_trust", "root": str(cwd), "trusted": False})
        revoked = await channel.until("project_trust")
        await channel.send({"action": "import", "path": str(source)})
        await channel.until("conversation")
        await channel.send({"action": "snapshot"})
        final = await channel.until("snapshot")
    exchange = context.root / "first-exchange.json"
    captured = json.loads(exchange.read_text(encoding="utf-8"))
    restart_exchange = json.loads((context.root / "restart-exchange.json").read_text(encoding="utf-8"))
    sdk = captured["sdk_inputs"]
    instructions = [next(message["instructions"] for message in reversed(item["messages"]) if message.get("instructions")) for item in sdk]
    correct_inputs = ("ROOT-GUIDANCE-7319" in instructions[0] and "NESTED-OVERRIDE-8421" in instructions[0]
        and "ROOT-GUI" in instructions[1] and "NESTED-OVERRIDE-8421" not in instructions[1])
    choices = [resumed["turn"]["project_trusted"], following["turn"]["project_trusted"]]
    exit_codes = [json.loads((context.root / f"{name}-process.json").read_text(encoding="utf-8"))["exit_code"] for name in ("first", "restart")]
    unchanged = source.read_bytes() == original
    survived = restarted["trusted_projects"] == [str(cwd.resolve())]
    removed = revoked["saved"] and not revoked["trusted"] and final["trusted_projects"] == []
    passed = (correct_inputs and choices == [False, True] and untrusted["trusted_projects"] == [] and trusted["saved"]
        and resumed["turn"]["status"] == following["turn"]["status"] == "completed"
        and unchanged and no_request and survived and removed and exit_codes == [0, 0]
        and len(captured["exchanges"]) == 2 and restart_exchange["exchanges"] == [])
    evidence = {"conversation": Path(opened["path"]), "exchange": exchange, "source": source,
        "restart_exchange": context.root / "restart-exchange.json", "root_guidance": repository / "AGENTS.md"}
    evidence.update({f"{name}.{kind}": context.root / f"{name}.{kind}.txt" for name in ("first", "restart") for kind in ("stdout", "stderr")})
    evidence.update({f"{name}.process": context.root / f"{name}-process.json" for name in ("first", "restart")})
    return CaseOutcome("passed" if passed else "failed", {"imported_source_unchanged": unchanged, "local_choice_applied": choices,
        "trust_survived_restart": survived, "revoked": removed, "no_request_before_resume": no_request,
        "restart_mock_http_attempts": len(restart_exchange["exchanges"]), "service_exit_codes": exit_codes,
        "correct_sources": correct_inputs, "mock_http_attempts": len(captured["exchanges"]),
        "shared_state": "one synthetic local trust store across explicit service restart"}, evidence)


async def _worker(fixture: Path) -> None:
    from myrk.desktop_service import serve
    from pydantic_ai import models

    material = json.loads(fixture.read_text(encoding="utf-8"))
    context = CaseContext(Path(material["root"]), material["protocol"], material["run_id"], material["case_id"])
    session = context.session([Reply(**item) for item in material["replies"]], config=ProviderConfig())
    directory = Path(material["directory"])
    inherited = {(turn.task.id, attempt.number) for path in directory.glob("*.json") for turn in Conversation.open(path).turns
        if turn.task for attempt in turn.request_attempts}
    models.ALLOW_MODEL_REQUESTS = True

    @asynccontextmanager
    async def factory(_key: str) -> AsyncIterator[Model]:
        marker = context.root / f"{material['name']}-model-open.json"
        if not marker.exists():
            marker.write_text(json.dumps({"opened_at": datetime.now(timezone.utc).isoformat(), "mock_http_attempts": len(session.exchanges)}), encoding="utf-8")
        async with session.model() as model:
            yield model

    await serve(directory, factory)
    attempts: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        conversation = Conversation.open(path)
        attempts.extend({"task_id": turn.task.id, **attempt.model_dump(mode="json")}
            for turn in conversation.turns if turn.task for attempt in turn.request_attempts if (turn.task.id, attempt.number) not in inherited)
    session.save(context.root / f"{material['name']}-exchange.json", attempts=attempts,
        run_id=context.run_id, case_id=context.case_id, task_id=attempts[-1]["task_id"] if attempts else "no-request")


def register(registry: CaseRegistry) -> None:
    for name in ("trusted", "untrusted"):
        registry.register(AcceptanceCase(f"trust.guidance-{name}", "project-trust", ("deepseek", "openai-chat", "google"), guidance))
    registry.register(AcceptanceCase("trust.import-local-choice", "project-trust", ("deepseek",), imported))
    for name in ("allow", "deny"):
        registry.register(AcceptanceCase(f"trust.recipient-{name}", "history-recipient", ("openai-chat",), recipient))


if __name__ == "__main__":
    asyncio.run(_worker(Path(sys.argv[1])))
