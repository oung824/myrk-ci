"""Local tool effects observed through real conversations and the executor."""

import asyncio
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from pydantic import TypeAdapter
from pydantic_ai import CancellationToken
from pydantic_ai.messages import ToolReturnPart

from myrk.actions import Action
from myrk.conversation import CheckCommand, Conversation, ConversationRecord, Task, TaskScope, Turn, Update
from myrk.executor import POWERSHELL
from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, CaseRegistry, Protocol
from tests.support.agent_acceptance.replay import AcceptanceSession, Reply


def known_save_failure(case_id: str, turn: Turn, *, path: Path, save_failures: tuple[str, ...],
                       protocol: Protocol | None = None) -> dict[str, Any] | None:
    """Match independently observed public shapes; never infer a shared cause or repair."""
    if turn.status != "limited" or turn.answer or turn.error not in save_failures:
        return None
    timestamp = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00"
    pattern = timestamp + "：进展未能保存；最后可靠保存位置：" + re.escape(str(path)) + "；时间：" + timestamp + "。"
    if turn.error is None or re.fullmatch(pattern, turn.error) is None:
        return None
    if case_id == "review.service-retry":
        if protocol != "deepseek" or turn.requests != 3 or turn.request_owner_id is not None or len(turn.actions) != 1:
            return None
        action = turn.actions[0]
        review = action.review
        target, proof = path.parent / "candidate.txt", path.parent / "materials/alternative.txt"
        if (action.kind != "create" or action.status != "cancelled" or action.effect != "unknown" or action.target != str(target)
            or action.delegation_id is not None or action.prepared_request != 1 or action.approval_basis != "assistant"
            or action.prepared_parameters != {"path": str(target), "content": "approved-candidate-bytes", "create_parents": [], "creation_parent": str(path.parent)}
            or review is None or review.status != "allowed" or review.attempts != 2 or review.unmeasured_requests != 1 or review.request_owner_id is not None
            or len(turn.request_attempts) != 1 or len(review.request_attempts) != 2):
            return None
        attempts = [*turn.request_attempts, *review.request_attempts]
        observed = [(item.number, item.own_number, item.actor, item.purpose, item.status, item.http_status, item.delegation_id) for item in attempts]
        if (observed != [(1, 1, "main", "work", "completed", None, None),
                        (2, 2, "action_review", "work", "failed", 503, None),
                        (3, 3, "action_review", "work", "completed", None, None)]
            or not attempts[1].error or attempts[0].error is not None or attempts[2].error is not None
            or target.exists() or not proof.is_file() or proof.read_bytes() != b"alternative-proof-evidence-5281"):
            return None
        return {"id": "conversation-progress-save-failure", "issue": ".scratch/acceptance-save-failure-diagnosis-20261113",
                "matched_public_symptom": True, "cause": "unknown"}
    shapes = {"actions.check": (0, []), "actions.files": (3, [("read", "completed"), ("create", "completed"), ("create", "cancelled")])}
    expected = shapes.get(case_id)
    if (expected is None or turn.requests != expected[0] or [(action.kind, action.status) for action in turn.actions] != expected[1]
        or [attempt.number for attempt in turn.request_attempts] != list(range(1, expected[0] + 1))
        or any(attempt.status != "completed" or attempt.actor != "main" or attempt.purpose != "work" for attempt in turn.request_attempts)):
        return None
    return {"id": "conversation-progress-save-failure", "issue": ".scratch/acceptance-save-failure-diagnosis-20261113",
            "matched_public_symptom": True, "cause": "unknown"}


def record_outcome(context: CaseContext, session: AcceptanceSession, conversation: Conversation,
                   task: Task, turn: Turn, passed: bool, *, facts: dict[str, Any] | None = None,
                   evidence: dict[str, Path] | None = None, children: list[Conversation] | None = None) -> CaseOutcome:
    """Keep original product attempts, including independently owned reviews."""
    saved_turns = [saved for account in [conversation, *(children or [])] for saved in account.turns
                   if saved.task and saved.task.id == task.id]
    attempts = {attempt.number: attempt for saved in saved_turns
                for attempt in [*saved.request_attempts, *(attempt for action in saved.actions if action.review
                                                          for attempt in action.review.request_attempts)]}
    exchange = session.save(context.root / "exchange.json",
        attempts=[attempt.model_dump(mode="json") for attempt in sorted(attempts.values(), key=lambda item: item.number)],
        run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    originals = {}
    missing_originals = []
    actions = {action.id: action for saved in saved_turns for action in saved.actions}
    for action in actions.values():
        produced_streams = {"stdout": bool(action.stdout), "stderr": bool(action.stderr)}
        for name, value in (("backup", action.backup_path), ("receipt", action.receipt_path),
                            ("stdout", action.stdout_path), ("stderr", action.stderr_path), ("full_output", action.full_output_path)):
            if value is not None:
                if Path(value).is_file():
                    originals[f"{action.id}.{name}"] = Path(value)
                else:
                    missing_originals.append({"action_id": action.id, "field": name, "path": value,
                        "required": produced_streams.get(name, True) and (action.status == "completed"
                            or action.status == "failed" and action.exit_code is not None and not action.timed_out)})
    known_defect = known_save_failure(context.case_id, turn, path=conversation.path, save_failures=conversation.save_failures, protocol=context.protocol)
    passed = passed and not any(item["required"] for item in missing_originals) and known_defect is None
    return CaseOutcome("passed" if passed else "failed", {
        "turn_status": turn.status, "error": turn.error, "answer": turn.answer,
        "product_attempts": task.total_requests, "main_product_attempts": task.requests, "mock_http_attempts": len(session.exchanges),
        "statuses": [action.status for action in turn.actions],
        "actions": [action.model_dump(mode="json") for action in turn.actions],
        "missing_action_evidence": missing_originals,
        "known_defect": known_defect,
        "historical_defect_status": "unresolved" if context.case_id in {"actions.check", "actions.files"}
            or context.case_id == "review.service-retry" and context.protocol == "deepseek" else None,
        **(facts or {}),
    }, {"conversation": conversation.path, "exchange": exchange,
        **{f"child.{index}.conversation": child.path for index, child in enumerate(children or [])},
        **originals, **(evidence or {})})


async def files(context: CaseContext) -> CaseOutcome:
    folder = context.root / "materials"
    folder.mkdir()
    source = folder / "notes.txt"
    original = b"\xef\xbb\xbffirst\r\nsecond\r\nuntouched\r\n"
    source.write_bytes(original)
    (folder / "proof.txt").write_text("independent-search-evidence-7319\n", encoding="utf-8")
    session = context.session([
        Reply(tool="read_text", arguments={"path": "notes.txt"}),
        Reply(tool="create_text", arguments={"path": "created.txt", "content": "first-version"}),
        Reply(tool="create_text", arguments={"path": "created.txt", "content": "must-not-overwrite"}),
        Reply(tool="edit_text", arguments={"path": "notes.txt", "old_text": "first\nsecond", "new_text": "updated\nsecond"}),
        Reply(tool="read_text", arguments={"path": "created.txt"}),
        Reply(tool="write_text", arguments={"path": "created.txt", "content": "whole-replacement"}),
        Reply(tool="delete_file", arguments={"path": "created.txt"}),
        Reply(tool="search_text", arguments={"query": "independent-search-evidence-7319"}),
        Reply(text="Synthetic file workflow completed; inspect the recorded bytes and actions."),
    ])
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Read and search the synthetic files; create, precisely edit, replace and delete only the specified files.",
        scope=TaskScope(root=str(folder), allowed_actions=["read", "edit", "list"], provider="deepseek", recipients=["deepseek"]),
        provider_config=session.config)
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model)
    edited = b"\xef\xbb\xbfupdated\r\nsecond\r\nuntouched\r\n"
    statuses = [action.status for action in turn.actions]
    backups = [Path(action.backup_path).read_bytes() for action in turn.actions if action.backup_path]
    passed = (turn.status == "completed" and statuses == ["completed", "completed", "failed", "completed", "completed", "completed", "completed", "completed"]
        and source.read_bytes() == edited and not (folder / "created.txt").exists()
        and backups == [original, b"first-version", b"whole-replacement"]
        and "independent-search-evidence-7319" in json.dumps(session.sdk_inputs[-1]))
    return record_outcome(context, session, conversation, task, turn, passed,
        facts={"final_bytes": source.read_bytes().hex(), "backup_bytes": [raw.hex() for raw in backups]},
        evidence={"source": source, "search_source": folder / "proof.txt"})


async def file_error(context: CaseContext) -> CaseOutcome:
    folder = context.root / "materials"
    folder.mkdir()
    source = folder / "notes.txt"
    source.write_bytes(b"invalid-prefix\xff\xfe" if context.case_id.endswith("failure") else b"prepared-original")
    conflict = context.case_id.endswith("conflict")
    session = context.session([
        Reply(tool="delete_file" if conflict else "read_text", arguments={"path": "notes.txt"}),
        Reply(text="The original error or changed version is retained."),
    ])
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Read only the synthetic file, or delete its explicitly reviewed version.",
        scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]), provider_config=session.config)
    if context.case_id.endswith("denied"):
        task.approval.access.denied_paths = [str(source)]
    if conflict:
        protected = folder / ".agents" / "notes.txt"
        protected.parent.mkdir()
        source.rename(protected)
        source = protected
        session = context.session([Reply(tool="delete_file", arguments={"path": ".agents/notes.txt"}), Reply(text="Changed version preserved.")])
    decisions: list[dict[str, Any]] = []

    async def approve(action: Action) -> bool:
        decisions.append(action.model_dump(mode="json"))
        source.write_bytes(b"changed-while-waiting")
        return True

    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model, approve_action=approve)
    action = turn.actions[0]
    expected = "conflict" if conflict else "failed"
    passed = (turn.status == "completed" and action.status == expected
        and source.read_bytes() == (b"changed-while-waiting" if conflict else b"prepared-original" if context.case_id.endswith("denied") else b"invalid-prefix\xff\xfe")
        and (len(decisions) == 1 and action.before_sha256 is not None if conflict else action.error is not None))
    if not conflict:
        error_marker = "拒绝访问。 (os error 5)" if context.case_id.endswith("denied") else "UTF-8"
        passed = (passed and action.error is not None and error_marker in action.error
            and ("prepared-original" if context.case_id.endswith("denied") else "invalid-prefix") not in json.dumps(session.sdk_inputs))
    return record_outcome(context, session, conversation, task, turn, passed,
        facts={"file_bytes": source.read_bytes().hex(), "decisions": decisions}, evidence={"source": source})


async def calculate(context: CaseContext) -> CaseOutcome:
    folder = context.root / "materials"
    folder.mkdir()
    invalid = context.case_id.endswith("invalid")
    source = folder / "amounts.csv"
    source.write_text("item,amount\napple,0.10\npear,0.20\nrefund," + ("invalid" if invalid else "-0.05") + "\n", encoding="utf-8")
    replies = [Reply(tool="describe_extension", arguments={"name": "calculate_table"}), Reply(text="Calculation schema discovered.")]
    session = context.session(replies)
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Sum the amount column exactly and retain the source rows; never estimate an invalid value.",
        scope=TaskScope(root=str(folder), allowed_actions=["read", "calculate"], provider="deepseek", recipients=["deepseek"]), provider_config=session.config)
    async with session.model() as model:
        await conversation.ask("Describe calculate_table before calling it.", document=None, scope=task.scope, task=task, model=model)
        history = ConversationRecord.model_validate_json(conversation.path.read_text(encoding="utf-8"))
        definition = TypeAdapter(dict[str, Any]).validate_python(next(part.content for message in history.messages for part in message.parts
                          if isinstance(part, ToolReturnPart) and part.tool_name == "describe_extension"))
        replies.extend([Reply(tool="call_extension", arguments={"name": "calculate_table", "schema_version": definition["schema_version"],
            "arguments": {"path": "amounts.csv", "column": "amount"}}), Reply(text="The calculation result is recorded by the tool.")])
        turn = await conversation.ask("Now run the described calculation.", document=None, scope=task.scope, task=task, model=model)
    history = ConversationRecord.model_validate_json(conversation.path.read_text(encoding="utf-8"))
    result = next(str(part.content) for message in history.messages for part in message.parts
                  if isinstance(part, ToolReturnPart) and part.tool_name == "call_extension")
    passed = (turn.status == "completed" and turn.actions[0].status == ("failed" if invalid else "completed")
        and ("未进行估算" in result if invalid else "0.25" in result and "[amounts.csv:4]" in result and "-0.05" in result))
    return record_outcome(context, session, conversation, task, turn, passed,
        facts={"calculation": result, "discovery_turns": 1, "execution_turns": 1,
               "same_task": all(saved.task is not None and saved.task.id == task.id for saved in conversation.turns)}, evidence={"table": source})


async def program(context: CaseContext) -> CaseOutcome:
    folder = context.root / "materials"
    folder.mkdir()
    is_command = ".command" in context.case_id
    stopped = context.case_id.endswith("stop")
    timeout = context.case_id.endswith("timeout")
    failure = context.case_id.endswith("failure")
    record_failure = context.case_id == "actions.record-write-failure"
    evidence_loss = context.case_id == "actions.check-missing-receipt"
    marker = folder / "started.txt"
    script = "[Console]::WriteLine('actual-stdout'); [Console]::Error.WriteLine('actual-stderr'); "
    if stopped or timeout:
        script += "[IO.File]::WriteAllText('started.txt','started'); Start-Sleep -Seconds 30"
    else:
        script += "exit " + ("7" if failure else "0")
    argv = [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script]
    session = context.session([Reply(tool="run_command" if is_command else "run_check",
        arguments={"program": argv[0], "arguments": argv[1:]} if is_command else {"name": "acceptance-check"}),
        Reply(text="Actual process output and exit status are retained.")])
    conversation = Conversation(context.root / "conversation.json")
    if record_failure:
        conversation.save()
        blocker = conversation.path.with_suffix(".json.tmp")
        blocker.mkdir()
        (blocker / "fixture.txt").write_text("This synthetic directory blocks the pending record file.", encoding="utf-8")
    task = Task(goal="Run the one specified synthetic command or check and report its actual exit status.",
        scope=TaskScope(root=str(folder), allowed_actions=["check"], provider="deepseek", recipients=["deepseek"],
            checks=[CheckCommand(name="acceptance-check", argv=argv, timeout_seconds=3 if timeout else 30)]), provider_config=session.config)
    token = CancellationToken()
    removed_receipt = context.root / "removed-receipt.json"

    def lose_completed_receipt(update: Update) -> None:
        action = update.action
        if action is not None and action.kind == "check" and action.status == "completed" and action.receipt_path and not removed_receipt.exists():
            receipt = Path(action.receipt_path)
            if not receipt.is_relative_to(context.root):
                raise ValueError("Synthetic evidence loss must remain inside its case.")
            removed_receipt.write_bytes(receipt.read_bytes())
            receipt.unlink()

    async def stop_after_start() -> None:
        while not marker.exists():
            await asyncio.sleep(.02)
        token.cancel()

    async with session.model() as model:
        stopper = asyncio.create_task(stop_after_start()) if stopped else None
        write_error: OSError | None = None
        try:
            turn = await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model, cancellation_token=token,
                on_update=lose_completed_receipt if evidence_loss else None)
        except OSError as error:
            write_error = error
            turn = conversation.turns[-1]
        finally:
            if stopper is not None:
                stopper.cancel()
                await asyncio.gather(stopper, return_exceptions=True)
    if not turn.actions:
        failed_turn = context.root / "failed-turn.json"
        failed_turn.write_text(json.dumps({"turn": turn.model_dump(mode="json"), "save_failures": conversation.save_failures,
            "last_saved_at": conversation.last_saved_at, "record_write_error_type": type(write_error).__name__ if write_error else None,
            "record_write_error": str(write_error) if write_error else None}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return record_outcome(context, session, conversation, task, turn, False,
            facts={"save_failures": list(conversation.save_failures), "planned_record_obstruction": record_failure,
                   "planned_negative": record_failure,
                   "expectation_met": record_failure and isinstance(write_error, PermissionError)
                       and write_error.filename == str(conversation.path.with_suffix(".json.tmp"))
                       and turn.status == "failed" and bool(conversation.save_failures) and task.requests == 0 and not session.exchanges,
                   "record_write_error_type": type(write_error).__name__ if write_error else None,
                   "record_write_error": str(write_error) if write_error else None}, evidence={"failed_turn": failed_turn})
    after_stop = len(session.exchanges)
    if stopped:
        await asyncio.sleep(.02)
    action = turn.actions[0]
    expected_status = "cancelled" if stopped else "failed" if failure or timeout else "completed"
    passed = (turn.status == ("cancelled" if stopped else "completed") and action.status == expected_status
        and action.exit_code is not None and action.cleanup_complete is True and action.sandbox_type == "windowsMxc"
        and "actual-stdout" in action.stdout and "actual-stderr" in action.stderr and action.timed_out is timeout
        and (action.exit_code == (7 if failure else 0) if not stopped and not timeout else marker.exists())
        and (not stopped or len(session.exchanges) == after_stop == 1))
    outcome = record_outcome(context, session, conversation, task, turn, passed, facts={"after_stop_attempts": after_stop},
        evidence={"removed_receipt": removed_receipt} if evidence_loss else None)
    if evidence_loss:
        outcome.facts.update({"planned_negative": True, "expectation_met": outcome.product_status == "failed"
            and removed_receipt.is_file() and any(item["field"] == "receipt" and item["required"] for item in outcome.facts["missing_action_evidence"])})
    return outcome


async def send(context: CaseContext) -> CaseOutcome:
    folder = context.root / "materials"
    folder.mkdir()
    source = folder / "payload.txt"
    source.write_bytes(b"exact-approved-send-body")
    name = context.case_id.removeprefix("send.")
    received: list[bytes] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(500 if name == "receiver-failure" else 204)
            self.end_headers()

        def log_message(self, _format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    replies = [] if name == "unread" else [Reply(tool="read_text", arguments={"path": "payload.txt"})]
    replies.extend([Reply(tool="send_text", arguments={"path": "payload.txt", "url": f"http://127.0.0.1:{server.server_port}/receive"}),
        Reply(text="The isolated receiver and action record determine whether anything was sent.")])
    session = context.session(replies)
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Send only the previously read exact synthetic payload to the isolated local receiver if approved.",
        scope=TaskScope(root=str(folder), allowed_actions=[], provider="deepseek", recipients=["deepseek"]), provider_config=session.config)
    token = CancellationToken()
    decisions: list[dict[str, Any]] = []

    async def approve(action: Action) -> bool:
        decisions.append(action.model_dump(mode="json"))
        if name == "stop":
            token.cancel()
        if name == "allow":
            source.write_bytes(b"changed-after-read-must-not-send")
        return name != "deny"

    try:
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model,
                approve_action=approve, cancellation_token=token)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    after_stop = len(session.exchanges)
    await asyncio.sleep(.02)
    action = turn.actions[-1]
    expected = "completed" if name == "allow" else "denied" if name == "deny" else "cancelled" if name == "stop" else "failed"
    expected_received = [b"exact-approved-send-body"] if name in {"allow", "receiver-failure"} else []
    passed = (turn.status == ("cancelled" if name == "stop" else "completed") and action.kind == "send" and action.status == expected
        and received == expected_received and not worker.is_alive() and len(decisions) == 1
        and (name != "stop" or after_stop == len(session.exchanges) == 2)
        and (name != "allow" or source.read_bytes() == b"changed-after-read-must-not-send" and "HTTP" in action.stdout))
    receiver = context.root / "receiver.json"
    receiver.write_text(json.dumps({"received": [body.hex() for body in received], "closed": not worker.is_alive(),
        "host": "127.0.0.1", "port": server.server_port, "expected_status": 500 if name == "receiver-failure" else 204}, indent=2) + "\n", encoding="utf-8")
    return record_outcome(context, session, conversation, task, turn, passed,
        facts={"received": [body.hex() for body in received], "decisions": decisions, "after_stop_attempts": after_stop},
        evidence={"receiver": receiver, "source": source})


def register(registry: CaseRegistry) -> None:
    registry.register(AcceptanceCase("actions.files", "file-effects", ("deepseek",), files))
    for name in ("read-failure", "read-denied", "version-conflict"):
        registry.register(AcceptanceCase(f"actions.{name}", "file-effects", ("deepseek",), file_error))
    for name in ("calculate", "calculate-invalid"):
        registry.register(AcceptanceCase(f"actions.{name}", "calculation", ("deepseek",), calculate))
    for name in ("command", "command-failure", "check", "check-failure", "check-timeout", "check-stop"):
        registry.register(AcceptanceCase(f"actions.{name}", "process-effects", ("deepseek",), program))
    registry.register(AcceptanceCase("actions.record-write-failure", "failure-reporting", ("deepseek",), program))
    registry.register(AcceptanceCase("actions.check-missing-receipt", "failure-reporting", ("deepseek",), program))
    for name in ("allow", "deny", "receiver-failure", "unread", "stop"):
        registry.register(AcceptanceCase(f"send.{name}", "controlled-send", ("deepseek",), send))
