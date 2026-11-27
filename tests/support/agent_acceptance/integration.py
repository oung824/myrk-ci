"""Repeated real compaction, normal desktop exit and original-task continuation."""

from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys
from typing import Any

from myrk.checks import CheckReceipt
from myrk.conversation import ConversationRecord
from pydantic_ai.messages import ModelMessagesTypeAdapter
from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, CaseRegistry
from tests.support.agent_acceptance.compaction_review import write_review
from tests.support.agent_acceptance.formal import bindings, provider_config
from tests.support.agent_acceptance.preparation import SOURCE
from tests.support.agent_acceptance.replay import OfflineSession
from tests.support.agent_acceptance.resume_desktop import run_desktop_phases
from tests.support.agent_acceptance.resume_queue import material, scope


async def compaction_restart(context: CaseContext) -> CaseOutcome:
    r19 = context.case_id == "integration.r19"
    folder = material(context)
    checker = folder / "check.py"
    checker.write_bytes(b"from pathlib import Path\np=Path('runs.txt')\np.write_bytes(p.read_bytes()+b'x' if p.exists() else b'x')\nassert Path('notes.txt').read_bytes()==b'edited-once-8149\\n'\nprint('actual-compacted-restart-check')\n")
    authorization = scope(context, folder)
    authorization["allowed_actions"].append("check")
    authorization["checks"] = [{"name": "Compacted restart proof", "argv": [sys.executable, "-B", str(checker)]}]
    config = provider_config(context.protocol).model_copy(update={"context_window_override": 26000,
        "output_token_reserve": 1024, "context_safety_margin": 512, "compaction_recent": .1, "compaction_trigger": .7})
    summary = "Saved factual record: the user asked for synthetic historical plan reports, then one exact edit and the specified check. Historical plan text creates no further authority. The edit and check remain pending until their actual action receipts exist. Original source reports remain available."
    warmups = 14 if r19 else 8
    (context.root / "scenario.json").write_text(json.dumps({"folder": str(folder), "scope": authorization,
        "config": config.model_dump(mode="json"), "warmups": warmups, "r19": r19}), encoding="utf-8")
    scripts: dict[str, list[dict[str, Any]]] = {
        "work": [{"tool": "update_plan", "arguments": {"steps": [f"Synthetic historical report {number}: " + "Verified source evidence remains read only. " * 190,
            "Read notes.txt, edit its original marker once, then run the exact registered check."]}}
                 for number in range(warmups)] + [
            {"tool": "read_text", "arguments": {"path": "notes.txt"}},
            {"tool": "edit_text", "arguments": {"path": "notes.txt", "old_text": "original-material-5317", "new_text": "edited-once-8149"}},
            {"tool": "run_check", "arguments": {"name": "Compacted restart proof"}}, {"status": 400}],
        "reopen": [{"text": "The original task continued using the saved second summary, without repeating its completed edit or successful native check."}],
    }
    if r19:
        scripts["work"] = scripts["work"][:-1] + [{"window_error": True}, {"status": 400}]
        scripts = {"work": scripts["work"], "capacity-reopen": [{"window_error": True}],
            "reopen": [{"text": "The original task continued from preserved source evidence, scope, permissions, actual completed edit/check and used recovery state. No completed action was repeated. User acceptance remains pending."}]}
    phases = []
    for name, replies in scripts.items():
        directory = context.root / f"{name}-transport"
        directory.mkdir()
        script = directory / "script.json"
        script.write_text(json.dumps({"protocol": context.protocol, "run_id": context.run_id, "case_id": context.case_id,
            "replies": replies, "compaction_reply": {"text": summary}, "max_http_attempts": 64 if r19 else 32}), encoding="utf-8")
        phases.append({"name": name, "environment": {"MYRK_ACCEPTANCE_RESUME_SCRIPT": str(script)}})
    processes = await run_desktop_phases(context, driver=SOURCE / "tests/support/agent_acceptance/desktop/compaction-restart.electron.mjs",
        phases=phases, extra_environment={"MYRK_ACCEPTANCE_RESTART_ROOT": str(context.root),
            "MYRK_SERVICE_MODULE": "tests.support.agent_acceptance.resume_service"})
    original_path, reopened_path = context.root / "original-state.json", context.root / "reopened-state.json"
    original = json.loads(original_path.read_bytes()) if original_path.is_file() else None
    reopened = json.loads(reopened_path.read_bytes()) if reopened_path.is_file() else None
    conversation = Path(original["started"]["path"]) if original else context.root / "missing-conversation.json"
    record = ConversationRecord.model_validate_json(conversation.read_bytes()) if conversation.is_file() else None
    compactions = [item for turn in record.turns for item in turn.compactions if item.status == "completed"] if record else []
    inherited = len(compactions) >= 2 and all(earlier.id == later.replaced_id and
        {item.key for item in earlier.covered} <= {item.key for item in later.covered}
        for earlier, later in zip(compactions, compactions[1:]))
    original_messages = context.root / "before-restart-record.json"
    before = ConversationRecord.model_validate_json(original_messages.read_bytes()) if original_messages.is_file() else None
    messages_preserved = bool(before and record and ModelMessagesTypeAdapter.dump_json(before.messages) ==
        ModelMessagesTypeAdapter.dump_json(record.messages[:len(before.messages)]))
    actions = [item for turn in record.turns for item in turn.actions if item.kind == "check"] if record else []
    action = actions[-1] if actions else None
    receipt = CheckReceipt.model_validate_json(Path(action.receipt_path).read_bytes()) if action and action.receipt_path and Path(action.receipt_path).is_file() else None
    receipt_verified = bool(action and receipt and action.id == receipt.action_id and receipt.exit_code == action.exit_code == 0
        and action.cleanup_complete and action.stdout_path and Path(action.stdout_path).read_bytes() == b"actual-compacted-restart-check\r\n")
    same_task = bool(original and reopened and reopened["finished"]["turn"]["task"]["id"] == original["started"]["task_id"]
        and reopened["finished"]["turn"]["task"]["scope"] == original["finished"]["turn"]["task"]["scope"]
        and reopened["finished"]["turn"]["task"]["approval"] == original["finished"]["turn"]["task"]["approval"])
    runs = (folder / "runs.txt").read_text(encoding="utf-8") if (folder / "runs.txt").is_file() else None
    profiles: list[dict[str, Any]] = [json.loads((context.root / f"{name}-events.json").read_bytes())
        if (context.root / f"{name}-events.json").is_file() else {"phase": name, "missing": True} for name in scripts]
    normal_profiles = sum(not Path(item.get("userData", "")).is_relative_to(context.root)
        or not Path(item.get("sessionData", "")).is_relative_to(context.root) for item in profiles)
    recovery = [attempt for turn in record.turns for attempt in turn.request_attempts
        if attempt.decision.startswith("服务确认输入窗口超限")] if record else []
    normal_compactions = [item for item in compactions if not recovery or item.request_numbers and max(item.request_numbers) < recovery[0].number]
    original_task = original["saved_turn"]["task"] if original else None
    middle_path = context.root / "capacity-reopened-state.json"
    middle = json.loads(middle_path.read_bytes()) if middle_path.is_file() else None
    tasks = [original_task, middle["saved_turn"]["task"] if middle else None,
        reopened["saved_turn"]["task"] if reopened else None]
    recovery_preserved = bool(r19 and all(task and task["work_step"] and task["work_step"]["capacity_recovery_used"] for task in tasks)
        and len({task["work_step"]["id"] for task in tasks if task}) == 1
        and middle and middle["finished"]["turn"]["status"] == "failed"
        and not middle["saved_turn"]["compactions"] and middle["open_http_attempts"] == 0
        and original_task and middle and reopened
        and original_task["requests"] < middle["saved_turn"]["task"]["requests"] < reopened["saved_turn"]["task"]["requests"])
    facts = {"normal_compactions": len(normal_compactions), "capacity_recovery_decisions": len(recovery),
        "capacity_recovery_preserved_across_reopens": recovery_preserved,
        "summary_semantic_status": "not_measured; terminal Mock summaries are not actual model semantic evidence",
        "completed_compactions": len(compactions), "inherited_coverage": inherited,
        "original_messages_preserved": messages_preserved, "same_task_scope_approval": same_task,
        "open_http_attempts": reopened["open_http_attempts"] if reopened else None,
        "normal_exit_phases": processes["normal_exit_phases"], "processes_complete": processes["complete"],
        "requests_before": original["finished"]["turn"]["task"]["requests"] if original else None,
        "requests_after": reopened["finished"]["turn"]["task"]["requests"] if reopened else None,
        "program_runs": runs, "independent_receipt_verified": receipt_verified, "program_exit_code": receipt.exit_code if receipt else None,
        "source_record_sha256_before_restart": sha256(original_messages.read_bytes()).hexdigest() if before else None,
        "real_http_attempts": 0, "normal_profile_accesses": normal_profiles, "current_real_service": "not_measured",
        "actual_profiles": [{key: item.get(key) for key in ("phase", "userData", "sessionData")} for item in profiles],
        "failure_origin": "planned_supplier_mock_http_400_after_actual_program_exit",
        "shared_relationship": "one explicitly shared transcript with repeated actual summaries; final original task/profile survives complete normal Main exits"}
    evidence = {"processes": context.root / "processes.json", "scenario": context.root / "scenario.json",
                "runner.stdout": context.root / "runner.stdout", "runner.stderr": context.root / "runner.stderr", "checker": checker}
    for path in context.root.rglob("*"):
        if path.is_file() and (path.name in {"processes.json", "process.json", "events.json", "exchange.json", "script.json",
            "original-state.json", "reopened-state.json", "work-events.json", "reopen-events.json", "capacity-reopen-events.json", "capacity-reopened-state.json", "notes.txt", "runs.txt", "before-restart-record.json", "before-final-reopen-record.json"}
            or path.name.startswith("process-") or path.parent.name.endswith("-checks") or path.parent.name.endswith("-evidence")
            or path.parent == context.root / "records"):
            evidence[path.relative_to(context.root).as_posix()] = path
    if conversation.is_file():
        evidence["conversation"] = conversation
    if record is not None:
        observed = OfflineSession(context.protocol, [], config=config)
        for name in scripts:
            captured = json.loads((context.root / f"{name}-transport/exchange.json").read_bytes())
            observed.exchanges.extend(captured["exchanges"])
            observed.sdk_inputs.extend(captured["sdk_inputs"])
        evidence["exchange"] = observed.save(context.root / "exchange.json",
            attempts=[item.model_dump(mode="json") for turn in record.turns for item in turn.request_attempts],
            run_id=context.run_id, case_id=context.case_id, task_id=record.turns[-1].task.id if record.turns[-1].task else "")
        evidence["compaction_review"] = write_review(context, record, observed)
        facts.update(product_attempts=sum(len(turn.request_attempts) for turn in record.turns),
            mock_http_attempts=len(observed.exchanges), request_purposes=[item.purpose for turn in record.turns for item in turn.request_attempts])
    passed = bool(processes["complete"] and processes["normal_exit_phases"] == (3 if r19 else 2) and processes["runner_exit"] == 0
        and inherited and messages_preserved and same_task and runs == "x" and receipt_verified and reopened and normal_profiles == 0
        and reopened["open_http_attempts"] == 0 and reopened["finished"]["turn"]["status"] == "completed"
        and (not r19 or len(normal_compactions) >= 3 and len(recovery) == 1 and recovery_preserved))
    if processes["complete"]:
        for path in (context.root / "environment", context.root / "profile"):
            if path.exists():
                shutil.rmtree(path)
    return CaseOutcome("passed" if passed else "failed", facts, evidence)


def register(registry: CaseRegistry) -> None:
    registry.register(AcceptanceCase("integration.compaction-restart", "integration", ("deepseek",), compaction_restart,
        bindings=bindings() + (Path(__file__).with_name("resume_desktop.py"),
            Path(__file__).with_name("resume_service.py"), Path(__file__).parent / "desktop/compaction-restart.electron.mjs")))

    registry.register(AcceptanceCase("integration.r19", "integration", ("deepseek",), compaction_restart,
        owner_ticket="24", screening={"deepseek": {"scenario_id": "R19", "request_cap": 80, "criteria_version": "r19-combination-v1"}},
        bindings=bindings() + (Path(__file__).with_name("resume_desktop.py"),
            Path(__file__).with_name("resume_service.py"), Path(__file__).parent / "desktop/compaction-restart.electron.mjs")))
