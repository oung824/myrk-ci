"""Reusable real-desktop normal-exit phases and original-task restart case."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter
from myrk.checks import CheckReceipt
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome
from tests.support.agent_acceptance.formal import build, environment, provider_config
from tests.support.agent_acceptance.preparation import SOURCE


async def run_desktop_phases(context: CaseContext, *, driver: Path, phases: list[dict[str, Any]],
                             extra_environment: dict[str, str]) -> dict[str, Any]:
    """Run each driver in a fresh Main, after all prior owned processes exited."""
    build_root = Path(os.environ.get("MYRK_CHECK_RUN_DIRECTORY", str(context.root))) / ("restart-" + uuid4().hex[:8])
    await asyncio.to_thread(build, build_root)
    request = context.root / "desktop-phases.json"
    request.write_text(json.dumps({"root": str(context.root), "driver": str(driver), "phases": phases,
        "environment": {"MYRK_TEST_RUN_DIRECTORY": str(build_root), "MYRK_USER_DATA_DIRECTORY": str(context.root / "profile"),
            "MYRK_DATA_DIRECTORY": str(context.root / "records"), **extra_environment}}, indent=2), encoding="utf-8")
    process = await asyncio.create_subprocess_exec("node", str(SOURCE / "tests/support/agent_acceptance/desktop/restart.mjs"),
        str(request), cwd=SOURCE, env=environment(context.root / "environment"), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    (context.root / "runner.stdout").write_bytes(stdout)
    (context.root / "runner.stderr").write_bytes(stderr)
    facts = TypeAdapter(dict[str, Any]).validate_json((context.root / "processes.json").read_bytes())
    facts["runner_exit"] = process.returncode
    for leaf in ("build.stdout", "build.stderr"):
        (context.root / leaf).write_bytes((build_root / leaf).read_bytes())
    if facts["complete"]:
        shutil.rmtree(build_root)
    return facts


async def desktop_restart(context: CaseContext) -> CaseOutcome:
    from tests.support.agent_acceptance.resume_queue import material, scope

    folder = material(context)
    check = folder / "check.py"
    check.write_bytes(b"from pathlib import Path\np=Path('runs.txt')\np.write_bytes(p.read_bytes()+b'x' if p.exists() else b'x')\nassert Path('notes.txt').read_bytes()==b'edited-once-8149\\n'\nprint('actual-program-exit-verified')\n")
    authorization = scope(context, folder)
    authorization["allowed_actions"].append("check")
    authorization["checks"] = [{"name": "Restart proof", "argv": [sys.executable, "-B", str(check)]}]
    config = provider_config(context.protocol).model_copy(update={"input_token_limit_override": 100000})
    (context.root / "scenario.json").write_text(json.dumps({"folder": str(folder), "scope": authorization,
        "config": config.model_dump(mode="json")}), encoding="utf-8")
    phases = []
    scripts = {"work": [{"tool": "read_text", "arguments": {"path": "notes.txt"}},
        {"tool": "edit_text", "arguments": {"path": "notes.txt", "old_text": "original-material-5317", "new_text": "edited-once-8149"}},
        {"tool": "run_check", "arguments": {"name": "Restart proof"}}, {"status": 400}],
        "reopen": [{"text": "The original task resumed without repeating its edit or successful check."}]}
    for name, replies in scripts.items():
        directory = context.root / f"{name}-transport"
        directory.mkdir()
        script = directory / "script.json"
        script.write_text(json.dumps({"protocol": context.protocol, "run_id": context.run_id, "case_id": context.case_id,
            "replies": replies}), encoding="utf-8")
        phases.append({"name": name, "environment": {"MYRK_ACCEPTANCE_RESUME_SCRIPT": str(script)}})
    processes = await run_desktop_phases(context, driver=SOURCE / "tests/support/agent_acceptance/desktop/resume.electron.mjs",
        phases=phases, extra_environment={"MYRK_ACCEPTANCE_RESTART_ROOT": str(context.root),
            "MYRK_SERVICE_MODULE": "tests.support.agent_acceptance.resume_service"})
    state_path, reopened_path = context.root / "original-state.json", context.root / "reopened-state.json"
    original = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    reopened = json.loads(reopened_path.read_text(encoding="utf-8")) if reopened_path.exists() else None
    record = json.loads(Path(original["started"]["path"]).read_text(encoding="utf-8")) if original else {"turns": []}
    check_actions = [action for turn in record["turns"] for action in turn["actions"] if action["kind"] == "check"]
    check_action = check_actions[-1] if check_actions else {}
    receipt = (CheckReceipt.model_validate_json(Path(check_action["receipt_path"]).read_bytes())
        if check_action.get("receipt_path") and Path(check_action["receipt_path"]).is_file() else None)
    receipt_verified = bool(receipt and receipt.action_id == check_action["id"] and receipt.exit_code == 0
        and Path(check_action["stdout_path"]).read_bytes() == b"actual-program-exit-verified\r\n")
    evidence = {"processes": context.root / "processes.json", "scenario": context.root / "scenario.json",
        "runner.stdout": context.root / "runner.stdout", "runner.stderr": context.root / "runner.stderr", "checker": check}
    for path in context.root.rglob("*"):
        if path.is_file() and (path.name in {"processes.json", "process.json", "events.json", "exchange.json", "script.json",
            "original-state.json", "reopened-state.json", "work-events.json", "reopen-events.json", "notes.txt", "runs.txt"}
            or path.name.startswith("process-") or path.parent.name.endswith("-checks") or path.parent.name.endswith("-evidence")
            or path.parent == context.root / "records"):
            evidence[path.relative_to(context.root).as_posix()] = path
    passed = (processes["complete"] and processes["normal_exit_phases"] == 2 and processes["runner_exit"] == 0
        and original is not None and reopened is not None and check_action.get("exit_code") == 0
        and check_action.get("cleanup_complete") is True and bool(check_action.get("receipt_path"))
        and receipt_verified)
    facts = {"normal_exit_phases": processes["normal_exit_phases"], "processes_complete": processes["complete"],
        "open_http_attempts": reopened["open_http_attempts"] if reopened else None,
        "requests_before": original["finished"]["turn"]["task"]["requests"] if original else None,
        "requests_after": reopened["finished"]["turn"]["task"]["requests"] if reopened else None,
        "program_exit_code": receipt.exit_code if receipt else None, "independent_receipt_verified": receipt_verified,
        "program_runs": (folder / "runs.txt").read_text(encoding="utf-8") if (folder / "runs.txt").exists() else None,
        "real_http_attempts": 0, "normal_profile_accesses": 0, "failure_origin": "supplier_mock_http_400_after_actual_program_exit",
        "current_real_service": "not_measured", "shared_relationship": "explicit original task/profile/records across complete normal application exits"}
    if processes["complete"]:
        for path in (context.root / "build/project", context.root / "environment", context.root / "profile"):
            if path.exists():
                shutil.rmtree(path)
    return CaseOutcome("passed" if passed else "failed", facts, evidence)
