"""Captured approval settings and independently enforced read-only review."""

import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic_ai import CancellationToken
from pydantic_ai.messages import ToolReturnPart

from myrk.actions import Action
from myrk.conversation import Conversation, Task, TaskScope
from myrk.executor import POWERSHELL
from myrk.permissions import ApprovalMode, ApprovalSettings
from tests.support.agent_acceptance.actions import record_outcome
from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, CaseRegistry, Protocol
from tests.support.agent_acceptance.replay import Reply


def decision(allow: bool = True) -> Reply:
    return Reply(text=json.dumps({"decision": "allow" if allow else "deny", "risk": "low" if allow else "high",
        "authorization": "high" if allow else "low", "rule": "general",
        "reason": "The exact candidate path, bytes and independent evidence were checked against the explicit user authorization.",
        "same_denied_outcome": False}))


async def approval(context: CaseContext) -> CaseOutcome:
    folder = context.root / "materials"
    folder.mkdir()
    outside = context.root / "outside.txt"
    outside.write_text("outside-readable-evidence-9041", encoding="utf-8")
    target = context.root / "candidate.txt"
    settings_change = context.case_id.endswith("settings")
    mode: ApprovalMode = "full_access" if context.case_id.endswith("full-access") else "auto_review" if ".auto-" in context.case_id else "request_approval"
    allow = not context.case_id.endswith("deny")
    replies = [Reply(tool="read_text", arguments={"path": str(outside)}),
               Reply(tool="create_text", arguments={"path": str(target), "content": "approved-exact-bytes"})]
    if mode == "auto_review":
        replies.append(decision(allow))
    if settings_change:
        replies.extend([Reply(tool="create_text", arguments={"path": str(context.root / "later.txt"), "content": "later-exact-bytes"}), decision()])
    replies.append(Reply(text="The recorded approval and actual bytes determine the result."))
    session = context.session(replies)
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Read the external synthetic evidence and create only the exact candidate file after the selected reviewer decides.",
        scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]),
        approval=ApprovalSettings(mode=mode), provider_config=session.config)
    captured: list[dict[str, Any]] = []

    async def approve(action: Action) -> bool:
        captured.append(action.model_dump(mode="json"))
        if settings_change:
            conversation.set_turn_reviewer(task.id, "assistant")
            conversation.set_approval_mode("full_access")
        return allow

    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model,
            review_model=model, approve_action=approve)
    read, action = turn.actions[:2]
    sandbox = "none" if mode == "full_access" else "windowsMxc"
    passed = (turn.status == "completed" and read.status == "completed" and read.sandbox_type == sandbox
        and action.status == ("completed" if allow else "denied") and target.exists() is allow
        and (not allow or target.read_bytes() == b"approved-exact-bytes")
        and len(captured) == (1 if mode == "request_approval" else 0)
        and action.approval_settings is not None and action.approval_settings.reviewer == ("none" if mode == "full_access" else "assistant" if mode == "auto_review" else "user")
        and action.approval_settings.condition == ("never" if mode == "full_access" else "on_request")
        and (action.review is not None and action.review.status == ("allowed" if allow else "denied") if mode == "auto_review" else action.review is None)
        and "outside-readable-evidence-9041" in json.dumps(session.sdk_inputs[1]))
    if settings_change:
        later = turn.actions[-1]
        reopened = Conversation.open(conversation.path)
        passed = (passed and action.approval_settings is not None and later.status == "completed" and later.approval_settings is not None
            and later.approval_settings.reviewer == "assistant" and later.approval_settings.access.sandbox == "restricted"
            and action.approval_settings.reviewer == "user" and captured[0]["approval_settings"] == action.approval_settings.model_dump(mode="json")
            and reopened.approval_settings.mode == "full_access" and reopened.turns[-1].approval_settings == turn.approval_settings
            and (context.root / "later.txt").read_bytes() == b"later-exact-bytes")
    return record_outcome(context, session, conversation, task, turn, passed,
        facts={"outside_read": outside.read_text(encoding="utf-8"), "human_decisions": captured, "review_requests": task.review_requests},
        evidence={"outside_source": outside})


async def review(context: CaseContext) -> CaseOutcome:
    folder = context.root / "materials"
    folder.mkdir()
    proof = folder / "alternative.txt"
    proof.write_text("alternative-proof-evidence-5281", encoding="utf-8")
    target = context.root / "candidate.txt"
    readonly_target = folder / "must-not-write.txt"
    name = context.case_id.removeprefix("review.")
    scripts = {
        "evidence-recovery": [Reply(tool="read_text", arguments={"path": "missing.txt"}),
            Reply(tool="read_text", arguments={"path": "alternative.txt"}), decision()],
        "readonly": [Reply(tool="run_command", arguments={"program": POWERSHELL, "arguments": ["-NoProfile", "-NonInteractive", "-Command",
            "[IO.File]::WriteAllText('must-not-write.txt','forbidden-write')"]}),
            Reply(tool="read_text", arguments={"path": "alternative.txt"}), decision(False)],
        "service-retry": [Reply(status=503), decision()],
        "service-failure": [Reply(status=503), Reply(status=503), Reply(status=503)],
        "stream-retry": [Reply(text="partial-review-output", interrupt=True), decision()],
        "timeout": [Reply(tool="run_command", arguments={"program": POWERSHELL,
            "arguments": ["-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 30"]})],
        "stop": [Reply(tool="read_text", arguments={"path": "alternative.txt"})],
    }
    session = context.session([Reply(tool="create_text", arguments={"path": str(target), "content": "approved-candidate-bytes"}),
        *scripts[name], Reply(text="Review outcome and original failures are retained.")])
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Create the exact candidate only after independent review; evidence errors require selecting and reading the alternative proof.",
        scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]),
        approval=ApprovalSettings(mode="auto_review"), provider_config=session.config, review_timeout_seconds=.3 if name == "timeout" else 90)
    token = CancellationToken()

    async def stop_during_review() -> None:
        while len(session.exchanges) < 2:
            await asyncio.sleep(.001)
        token.cancel()

    async with session.model() as model:
        stopper = asyncio.create_task(stop_during_review()) if name == "stop" else None
        try:
            turn = await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model,
                review_model=model, cancellation_token=token)
        finally:
            if stopper is not None:
                stopper.cancel()
                await asyncio.gather(stopper, return_exceptions=True)
    after_stop = len(session.exchanges)
    await asyncio.sleep(.02)
    action = turn.actions[0]
    record = action.review
    allowed = name in {"evidence-recovery", "service-retry", "stream-retry"}
    expected = "allowed" if allowed else "denied" if name == "readonly" else "failed" if name == "service-failure" else "timeout" if name == "timeout" else "cancelled"
    passed = (record is not None and record.status == expected and target.exists() is allowed
        and (not allowed or target.read_bytes() == b"approved-candidate-bytes") and not readonly_target.exists()
        and action.status == ("completed" if allowed else "denied" if name == "readonly" else "cancelled" if name == "stop" else "failed")
        and turn.status == ("cancelled" if name == "stop" else "completed")
        and all(attempt.actor == "action_review" and attempt.purpose == "work" and attempt.delegation_id is None
                for attempt in record.request_attempts)
        and (name != "stop" or len(session.exchanges) == after_stop == 2))
    if record is not None:
        rendered = json.dumps(session.sdk_inputs, ensure_ascii=False)
        if name == "evidence-recovery":
            passed = (passed and record.attempts == 3
                and "未取得本次证据" in json.dumps(session.sdk_inputs[2], ensure_ascii=False)
                and "ExecutorRequestError" in json.dumps(session.sdk_inputs[2])
                and "alternative-proof-evidence-5281" in json.dumps(session.sdk_inputs[3]))
        if name in {"service-retry", "service-failure", "stream-retry"}:
            passed = passed and record.request_attempts[0].status == "failed" and record.request_attempts[0].error is not None
        if name in {"service-retry", "service-failure"}:
            failures = record.request_attempts[:-1] if name == "service-retry" else record.request_attempts
            passed = (passed and record.attempts == (2 if name == "service-retry" else 3)
                and all(attempt.http_status == 503 and attempt.status == "failed" for attempt in failures)
                and all(exchange["status"] == 503 for exchange in session.exchanges[1:1 + len(failures)]))
        if name == "stream-retry":
            passed = (passed and record.attempts == 2 and "partial-review-output" in session.exchanges[1]["received_utf8"]
                and session.exchanges[1]["error"] == "synthetic interruption after delivered output")
        if name == "readonly":
            inspections = [json.loads(str(part.content)) for message in record.messages for part in message.parts
                           if isinstance(part, ToolReturnPart) and part.tool_name == "run_command"]
            passed = (passed and len(inspections) == 1 and inspections[0]["exitCode"] != 0
                and "alternative-proof-evidence-5281" in rendered)
        review_input = session.sdk_inputs[1]
        tools = {tool["name"] for tool in review_input["parameters"]["function_tools"]}
        materials = next(json.loads(part["content"]) for message in review_input["messages"] for part in message["parts"]
                         if part["part_kind"] == "user-prompt")
        passed = (passed and tools == {"read_text", "run_command"}
            and materials["action"]["target"] == str(target)
            and materials["action"]["prepared_parameters"]["content"] == "approved-candidate-bytes"
            and action.prepared_action_sha256 is not None and action.approval_context_sha256 is not None
            and materials["action"]["prepared_action_sha256"] == action.prepared_action_sha256
            and materials["action"]["approval_context_sha256"] == action.approval_context_sha256)
    return record_outcome(context, session, conversation, task, turn, passed,
        facts={"review_status": record.status if record else None, "review_requests": task.review_requests,
            "after_stop_attempts": after_stop}, evidence={"proof": proof})


def register(registry: CaseRegistry) -> None:
    bindings = (Path(__file__).with_name("actions.py"),)
    for name in ("request-allow", "request-deny", "auto-allow", "auto-deny", "full-access", "running-settings"):
        registry.register(AcceptanceCase(f"approval.{name}", "approval-modes", ("deepseek",), approval, bindings))
    for name in ("evidence-recovery", "readonly", "service-retry", "service-failure", "stream-retry", "timeout", "stop"):
        protocols: tuple[Protocol, ...] = ("deepseek", "openai-chat", "google") if name == "evidence-recovery" else ("deepseek",)
        registry.register(AcceptanceCase(f"review.{name}", "independent-review", protocols, review, bindings))
