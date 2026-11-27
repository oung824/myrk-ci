"""Supplement, queue, parallel and original-task recovery acceptance cases."""

import asyncio
import json
from pathlib import Path
from typing import Any

from myrk.conversation import Conversation, Task, TaskScope
from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, CaseRegistry
from tests.support.agent_acceptance.formal import provider_config
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.service_channel import ServiceChannel


def material(context: CaseContext, name: str = "materials") -> Path:
    folder = context.root / name
    folder.mkdir()
    (folder / ".git").mkdir()
    (folder / "AGENTS.md").write_text("Synthetic acceptance project. Use only the explicitly authorized files.\n", encoding="utf-8")
    (folder / "notes.txt").write_bytes(b"original-material-5317\n")
    return folder


def scope(context: CaseContext, folder: Path) -> dict[str, Any]:
    recipient = "google" if context.protocol == "google" else "openai-compatible" if context.protocol == "openai-chat" else "deepseek"
    return {"root": str(folder), "allowed_actions": ["read", "edit"], "provider": recipient, "recipients": [recipient]}


async def supplement(context: CaseContext) -> CaseOutcome:
    folder = material(context)
    async with ServiceChannel(context, "service", [
        Reply(tool="read_text", arguments={"path": "notes.txt"}),
        Reply(tool="read_text", arguments={"path": "notes.txt"}),
        Reply(text="Original finished with the one supplement."), Reply(text="Queued task finished.")], gates=[0]) as channel:
        await channel.configure()
        await channel.send({"action": "send", "prompt": "Read the original material.", "scope": scope(context, folder)})
        started = await channel.receive("started")
        await channel.arrived(0)
        await channel.send({"action": "send", "prompt": "supplement-once-8149", "path": started["path"]})
        received = await channel.receive("supplement", state="received")
        await channel.send({"action": "send", "prompt": "queued-goal-9271", "new_task": True, "path": started["path"]})
        queued = await channel.receive("queue")
        channel.release(0)
        first = await channel.receive("finished", task_id=started["task_id"])
        following = await channel.receive("started")
        second = await channel.receive("finished", task_id=following["task_id"])
        await channel.send({"action": "open", "path": started["path"]})
        opened = await channel.receive("conversation")
    applied = [item for item in channel.events if item["event"] == "supplement" and item["state"] == "applied"]
    exchange = json.loads((channel.root / "exchange.json").read_text(encoding="utf-8"))
    input_counts = [item["request_utf8"].count("supplement-once-8149") for item in exchange["exchanges"][:3]]
    queue_after_finish = channel.events.index(first) < channel.events.index(following)
    passed = (first["turn"]["status"] == second["turn"]["status"] == "completed" and len(applied) == 1
        and applied[0]["supplement"]["id"] == received["supplement"]["id"]
        and first["turn"]["task"]["supplements"][0]["applied_request"] == 2
        and len(first["turn"]["task"]["supplements"]) == 1 and queue_after_finish
        and following["task_id"] == queued["queue"][0]["task"]["id"] != started["task_id"]
        and input_counts == [0, 1, 1] and opened["queue"] == [] and len(opened["turns"]) == 2
        and [action["status"] for action in first["turn"]["actions"]] == ["completed"])
    return CaseOutcome("passed" if passed else "failed", {"applied_events": len(applied), "queue_after_finish": queue_after_finish,
        "supplement_input_counts": input_counts,
        "task_id": started["task_id"], "queued_task_id": following["task_id"], "first": first["turn"], "second": second["turn"],
        "real_http_attempts": 0, "normal_profile_accesses": 0, "shared_relationship": "one conversation with a supplement and explicitly queued independent task"},
        {**channel.evidence(), "conversation": Path(started["path"]), "material": folder / "notes.txt", "guide": folder / "AGENTS.md"})


async def saved_queue(context: CaseContext) -> CaseOutcome:
    async with ServiceChannel(context, "stopped-service", [Reply(text="Held original response.")], gates=[0]) as initial:
        await initial.configure()
        await initial.send({"action": "send", "prompt": "Original must stop before the queued work."})
        started = await initial.receive("started")
        await initial.arrived(0)
        await initial.send({"action": "send", "path": started["path"], "prompt": "Saved queued work.", "new_task": True})
        queued = await initial.receive("queue")
        await initial.send({"action": "stop", "path": started["path"]})
        stopped = await initial.receive("finished", task_id=started["task_id"])
    async with ServiceChannel(context, "fresh-service", [Reply(text="Explicitly started saved work.")]) as reopened:
        await reopened.send({"action": "open", "path": started["path"]})
        opened = await reopened.receive("conversation")
        open_count = len(json.loads((reopened.root / "exchange.json").read_text(encoding="utf-8"))["exchanges"])
        await reopened.configure()
        await reopened.send({"action": "start_next", "path": started["path"]})
        finished = await reopened.receive("finished")
    identity = queued["queue"][0]["task"]["id"] == opened["queue"][0]["task"]["id"] == finished["task_id"]
    passed = (identity and open_count == 0 and stopped["turn"]["status"] == "cancelled"
        and len([item for item in initial.events if item["event"] == "started"]) == 1
        and opened["running"] is False and opened["turns"][-1]["task"] == stopped["turn"]["task"]
        and finished["turn"]["status"] == "completed" and finished["turn"]["task"]["requests"] == 1)
    return CaseOutcome("passed" if passed else "failed", {"open_http_attempts": open_count, "original_status": stopped["turn"]["status"],
        "queued_identity_preserved": identity, "real_http_attempts": 0, "normal_profile_accesses": 0,
        "shared_relationship": "explicit saved queue and stopped original conversation across normal service exits"},
        {**initial.evidence(), **reopened.evidence(), "conversation": Path(started["path"])})


async def parallel(context: CaseContext) -> CaseOutcome:
    first_folder, second_folder = material(context, "first-materials"), material(context, "second-materials")
    (second_folder / "notes.txt").write_text("second-isolated-material-9271\n", encoding="utf-8")
    async with ServiceChannel(context, "service", [Reply(text="First response must remain held."),
            Reply(text="Second response must remain held.")], gates=[0, 1]) as channel:
        await channel.configure()
        await channel.send({"action": "send", "prompt": "first-isolated-goal-5317", "scope": scope(context, first_folder)})
        first = await channel.receive("started")
        await channel.arrived(0)
        await channel.send({"action": "new"})
        fresh = await channel.receive("conversation")
        await channel.send({"action": "send", "prompt": "second-isolated-goal-9271", "path": fresh["path"], "scope": scope(context, second_folder)})
        second = await channel.receive("started")
        await channel.arrived(1)
        await channel.send({"action": "stop", "path": first["path"]})
        first_finished = await channel.receive("finished", path=first["path"])
        await channel.send({"action": "snapshot"})
        snapshot = await channel.receive("snapshot")
        second_running = next(item for item in snapshot["conversations"] if item["path"] == second["path"])["running"]
        await channel.send({"action": "stop", "path": second["path"]})
        second_finished = await channel.receive("finished", path=second["path"])
    exchange = json.loads((channel.root / "exchange.json").read_text(encoding="utf-8"))
    bodies = [item["request_utf8"] for item in exchange["exchanges"]]
    isolated = (len(bodies) == 2 and "second-isolated-goal-9271" not in bodies[0] and "first-isolated-goal-5317" not in bodies[1]
        and str(first_folder).replace("\\", "\\\\") not in bodies[1] and str(second_folder).replace("\\", "\\\\") not in bodies[0])
    statuses = [item["turn"]["status"] for item in (first_finished, second_finished)]
    separate = first["path"] != second["path"] and first["task_id"] != second["task_id"]
    passed = (separate and isolated and second_running and statuses == ["cancelled", "cancelled"]
        and [item["turn"]["task"]["requests"] for item in (first_finished, second_finished)] == [1, 1]
        and all(not item["turn"]["answer"] and not item["turn"]["actions"] for item in (first_finished, second_finished)))
    return CaseOutcome("passed" if passed else "failed", {"separate_tasks": separate, "inputs_isolated": isolated,
        "second_running_after_first_stop": second_running, "statuses": statuses, "http_attempts_after_stop": len(bodies),
        "task_ids": [first["task_id"], second["task_id"]], "real_http_attempts": 0, "normal_profile_accesses": 0,
        "shared_relationship": "only the service and terminal transport; conversations, folders and task accounts remain distinct"},
        {**channel.evidence(), "first.conversation": Path(first["path"]), "second.conversation": Path(second["path"]),
         "first.material": first_folder / "notes.txt", "second.material": second_folder / "notes.txt"})


async def original(context: CaseContext) -> CaseOutcome:
    folder = material(context)
    target = folder / "notes.txt"
    async with ServiceChannel(context, "interrupted", [Reply(tool="read_text", arguments={"path": "notes.txt"}),
            Reply(tool="edit_text", arguments={"path": "notes.txt", "old_text": "original-material-5317", "new_text": "edited-once-8149"})],
            interrupt_after_edit=True) as initial:
        await initial.configure()
        await initial.send({"action": "send", "prompt": "Read and edit notes once, then verify it.", "scope": scope(context, folder)})
        started = await initial.receive("started")
        await asyncio.wait_for(initial.process.wait(), 15)
    path = Path(started["path"])
    before_path = context.root / "original-before.json"
    before_path.write_bytes(path.read_bytes())
    pending_path = context.root / "interrupted-pending.json"
    pending_path.write_bytes(path.with_suffix(".json.tmp").read_bytes())
    before = json.loads(before_path.read_text(encoding="utf-8"))
    async with ServiceChannel(context, "reopened", [Reply(tool="read_text", arguments={"path": "notes.txt"}),
            Reply(text="The verified original edit was retained; it was not repeated.")]) as reopened:
        await reopened.send({"action": "open", "path": str(path)})
        opened = await reopened.receive("conversation")
        open_count = len(json.loads((reopened.root / "exchange.json").read_text(encoding="utf-8"))["exchanges"])
        await reopened.configure()
        await reopened.send({"action": "resume_task", "path": str(path), "task_id": started["task_id"]})
        blocked = await reopened.receive("error")
        blocked_count = len(json.loads((reopened.root / "exchange.json").read_text(encoding="utf-8"))["exchanges"])
        await reopened.send({"action": "import", "path": str(before_path)})
        imported = await reopened.receive("conversation")
        await reopened.send({"action": "verify_recovery", "path": imported["path"], "task_id": started["task_id"]})
        unowned = await reopened.receive("recovery")
        await reopened.send({"action": "resume_task", "path": imported["path"], "task_id": started["task_id"]})
        imported_blocked = await reopened.receive("error")
        await reopened.send({"action": "open", "path": str(path)})
        await reopened.receive("conversation")
        await reopened.send({"action": "verify_recovery", "path": str(path), "task_id": started["task_id"]})
        verified = await reopened.receive("recovery")
        await reopened.send({"action": "resume_task", "path": str(path), "task_id": started["task_id"]})
        finished = await reopened.receive("finished", task_id=started["task_id"])
    old_task = before["turns"][-1]["task"]
    new_task = finished["turn"]["task"]
    new_actions = [action["kind"] for action in finished["turn"]["actions"]]
    effect = verified["recovery"]["actions"][-1]["effect"]
    passed = (initial.process.returncode == 71 and opened["recovery"]["actions"][-1]["effect"] == "unknown"
        and open_count == blocked_count == 0 and bool(blocked["message"]) and bool(imported_blocked["message"])
        and unowned["recovery"]["can_resume"] is False and unowned["recovery"]["actions"][-1]["effect"] == "unknown"
        and verified["recovery"]["can_resume"] and effect == "completed" and finished["turn"]["status"] == "completed"
        and old_task["id"] == new_task["id"] == started["task_id"] and old_task["scope"] == new_task["scope"]
        and old_task["approval"] == new_task["approval"] and old_task["requests"] == 2 and new_task["requests"] == 4
        and new_task["total_requests"] == 4 and new_task["active_seconds"] >= old_task["active_seconds"]
        and new_actions == ["read"] and target.read_bytes() == b"edited-once-8149\n"
        and before["turns"][0]["actions"] == json.loads(path.read_text(encoding="utf-8"))["turns"][0]["actions"])
    return CaseOutcome("passed" if passed else "failed", {"planned_local_exit": initial.process.returncode,
        "failure_origin": "planned_local_process_interruption", "supplier_mock_fault": False, "current_real_service": "not_measured",
        "original_task_id": old_task["id"], "resumed_task_id": new_task["id"], "requests_before": old_task["requests"],
        "requests_after": new_task["requests"], "open_http_attempts": open_count, "blocked_resume_http_attempts": blocked_count,
        "imported_unknown_blocks_resume": not unowned["recovery"]["can_resume"], "verified_effect": effect,
        "new_action_kinds": new_actions, "real_http_attempts": 0, "normal_profile_accesses": 0,
        "pending_evidence_bindings": {str(path.with_suffix(".json.tmp")): pending_path.relative_to(context.root).as_posix()},
        "shared_relationship": "explicit original task, original local recovery ownership and imported unowned copy"},
        {**initial.evidence(), **reopened.evidence(), "conversation": path, "original": before_path,
         "imported.conversation": Path(imported["path"]), "material": target,
         "local_exit": initial.root / "planned-local-exit.json", "interrupted_pending": pending_path})


async def legacy(context: CaseContext) -> CaseOutcome:
    folder = material(context)
    config = provider_config(context.protocol).model_copy(update={"input_token_limit_override": 100000})
    session = context.session([Reply(status=400)], config=config)
    source = Conversation(context.root / "actual-failed-source.json")
    task = Task(goal="Keep the original task and one spent request when importing the legacy format.",
        scope=TaskScope.model_validate(scope(context, folder)), provider_config=config, request_limit=1)
    async with session.model() as model:
        failure = await source.ask(task.goal, document=None, scope=task.scope, task=task, model=model)
    failed_exchange = session.save(context.root / "failed-exchange.json", attempts=[item.model_dump(mode="json") for item in failure.request_attempts],
        run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    value = json.loads(source.path.read_text(encoding="utf-8"))
    value["version"] = 5
    for turn in value["turns"]:
        for field in ("request_owner_id", "request_account", "execution_scope", "execution_facts"):
            turn.pop(field, None)
        for field in ("request_policy", "total_requests", "delegation_max_turns", "max_turns", "turns_used"):
            turn["task"].pop(field, None)
    legacy_source = context.root / "legacy-source-v5.json"
    original_bytes = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    legacy_source.write_bytes(original_bytes)
    async with ServiceChannel(context, "import-service", [Reply(text="The same legacy task continued after an explicit increase.")]) as channel:
        await channel.send({"action": "import", "path": str(legacy_source)})
        imported = await channel.receive("conversation")
        await channel.send({"action": "open", "path": imported["path"]})
        opened = await channel.receive("conversation")
        open_count = len(json.loads((channel.root / "exchange.json").read_text(encoding="utf-8"))["exchanges"])
        await channel.configure()
        await channel.send({"action": "verify_recovery", "path": imported["path"], "task_id": task.id})
        await channel.receive("recovery")
        await channel.send({"action": "resume_task", "path": imported["path"], "task_id": task.id})
        blocked = await channel.receive("error")
        exhausted_count = len(json.loads((channel.root / "exchange.json").read_text(encoding="utf-8"))["exchanges"])
        await channel.send({"action": "continue_task", "path": imported["path"], "task_id": task.id, "request_limit": 3})
        finished = await channel.receive("finished", task_id=task.id)
    old_task = opened["turns"][-1]["task"]
    new_task = finished["turn"]["task"]
    copied = Path(imported["path"])
    backups = [path for path in (copied.parent / "backups").glob(f"{copied.stem}-v5-*.json")]
    passed = (failure.service_failure and failure.status == "failed" and old_task["id"] == new_task["id"] == task.id
        and old_task["request_policy"] == new_task["request_policy"] == "shared"
        and old_task["requests"] == old_task["request_limit"] == 1 and new_task["requests"] == new_task["total_requests"] == 2
        and new_task["request_limit"] == 3 and old_task["scope"] == new_task["scope"]
        and old_task["approval"] == new_task["approval"] and "上限" in blocked["message"]
        and open_count == exhausted_count == 0 and legacy_source.read_bytes() == original_bytes
        and len(backups) == 1 and backups[0].read_bytes() == original_bytes and finished["turn"]["status"] == "completed")
    return CaseOutcome("passed" if passed else "failed", {"source_unchanged": legacy_source.read_bytes() == original_bytes,
        "request_policy": new_task["request_policy"], "requests_before": old_task["requests"], "requests_after": new_task["requests"],
        "open_http_attempts": open_count, "exhausted_http_attempts": exhausted_count, "original_task_id": task.id,
        "resumed_task_id": new_task["id"], "failure_origin": "supplier_mock_http_400", "current_real_service": "not_measured",
        "real_http_attempts": 0, "normal_profile_accesses": 0, "shared_relationship": "explicit legacy import of an actual failed task"},
        {**channel.evidence(), "source": legacy_source, "actual_source": source.path, "conversation": copied,
         "failed_exchange": failed_exchange, **{f"backup.{index}": path for index, path in enumerate(backups)}})


async def delegated(context: CaseContext) -> CaseOutcome:
    folder = material(context)
    async with ServiceChannel(context, "delegated", [
        Reply(tool="delegate_work", arguments={"goal": "Read the declared file", "context": "Use the parent scope and adopt its later supplement once.",
            "evidence_requirements": "Keep the actual read, child record and independent account.", "expected_files": ["notes.txt"]}),
        Reply(tool="read_text", arguments={"path": "notes.txt"}), Reply(tool="read_text", arguments={"path": "notes.txt"}),
        Reply(text="The child retained the one supplement and actual read."), Reply(status=400)], gates=[1]) as initial:
        await initial.configure()
        await initial.send({"action": "send", "prompt": "Delegate the read, adopt my supplement, and retain the result.", "scope": scope(context, folder)})
        started = await initial.receive("started")
        await initial.arrived(1)
        await initial.send({"action": "send", "path": started["path"], "prompt": "delegated-supplement-once-3519"})
        await initial.receive("supplement", state="received")
        initial.release(1)
        failed = await initial.receive("finished", task_id=started["task_id"])
    path = Path(started["path"])
    work = failed["turn"]["delegations"][0]
    child_path = Path(work["record_path"])
    child_bytes = child_path.read_bytes()
    child = json.loads(child_bytes)
    child_record = Conversation.open(child_path)
    child_resume_error = ""
    try:
        child_record.prepare_resume(started["task_id"])
    except ValueError as error:
        child_resume_error = str(error)
    original_path = context.root / "original-parent.json"
    original_path.write_bytes(path.read_bytes())
    async with ServiceChannel(context, "resumed", [Reply(tool="read_text", arguments={"path": "notes.txt"}),
            Reply(text="The parent resumed its original task and retained the independently owned child record.")]) as resumed:
        await resumed.send({"action": "open", "path": str(child_path)})
        refused = await resumed.receive("error")
        await resumed.configure()
        await resumed.send({"action": "open", "path": str(path)})
        await resumed.receive("conversation")
        await resumed.send({"action": "verify_recovery", "path": str(path), "task_id": started["task_id"]})
        verification = await resumed.receive("recovery")
        await resumed.send({"action": "resume_task", "path": str(path), "task_id": started["task_id"]})
        finished = await resumed.receive("finished", task_id=started["task_id"])
    applied = [item for item in initial.events if item["event"] == "supplement" and item["state"] == "applied"]
    exchange = json.loads((initial.root / "exchange.json").read_text(encoding="utf-8"))
    input_counts = [item["request_utf8"].count("delegated-supplement-once-3519") for item in exchange["exchanges"]]
    task = finished["turn"]["task"]
    attempts = child["turns"][-1]["request_attempts"]
    passed = (failed["turn"]["service_failure"] and work["status"] == "completed" and len(applied) == 1 and input_counts == [0, 0, 1, 1, 1]
        and child["turns"][-1]["request_owner_id"] == child_record.turns[-1].request_owner_id == work["id"]
        and child["turns"][-1]["task"]["id"] == task["id"] == started["task_id"]
        and work["requests"] == 3 and work["turns_used"] == 3 and work["max_turns"] == 250
        and task["requests"] == 4 and task["total_requests"] == 7 and task["max_turns"] is None
        and all(item["actor"] == "delegation" and item["delegation_id"] == work["id"] for item in attempts)
        and "子助理" in child_resume_error and "本机会话列表" in refused["message"] and verification["recovery"]["can_resume"]
        and child_path.read_bytes() == child_bytes and finished["turn"]["status"] == "completed"
        and task["scope"] == failed["turn"]["task"]["scope"] and task["approval"] == failed["turn"]["task"]["approval"])
    return CaseOutcome("passed" if passed else "failed", {"applied_events": len(applied), "child_record_unchanged": child_path.read_bytes() == child_bytes,
        "supplement_input_counts": input_counts,
        "task_id": task["id"], "delegation_id": work["id"], "child_requests": work["requests"], "main_requests_after": task["requests"],
        "total_requests_after": task["total_requests"], "original_status": failed["turn"]["status"], "child_resume_error": child_resume_error,
        "failure_origin": "supplier_mock_http_400_after_completed_child", "current_real_service": "not_measured",
        "real_http_attempts": 0, "normal_profile_accesses": 0,
        "shared_relationship": "actual delegate_work parent/child, shared supplement and root task identity; independent child account"},
        {**initial.evidence(), **resumed.evidence(), "conversation": path, "child.conversation": child_path,
         "original_parent": original_path, "material": folder / "notes.txt"})


def register(registry: CaseRegistry) -> None:
    from tests.support.agent_acceptance.formal import bindings
    from tests.support.agent_acceptance.resume_desktop import desktop_restart

    registry.register(AcceptanceCase("queue.supplement", "task-control", ("deepseek", "openai-chat", "google"), supplement))
    registry.register(AcceptanceCase("queue.saved-stop", "task-control", ("deepseek",), saved_queue))
    registry.register(AcceptanceCase("parallel.stop", "task-control", ("deepseek", "openai-chat", "google"), parallel))
    registry.register(AcceptanceCase("recovery.original", "recovery", ("deepseek", "openai-chat", "google"), original))
    registry.register(AcceptanceCase("recovery.legacy", "recovery", ("deepseek", "openai-chat", "google"), legacy))
    registry.register(AcceptanceCase("recovery.delegation", "recovery", ("deepseek", "openai-chat", "google"), delegated))
    registry.register(AcceptanceCase("recovery.desktop-restart", "recovery-desktop", ("deepseek",), desktop_restart, bindings=bindings()))
