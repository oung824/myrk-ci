"""Frozen connection/settings materials and facts from the real public desktop path."""

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal

from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, Protocol
from tests.support.agent_acceptance.eligibility import deepseek_thinking_disabled
from tests.support.agent_acceptance.replay import Reply

MATERIALS = Path(__file__).resolve().parents[2] / "acceptance/materials/connection-desktop"
CRITERIA = "connection-desktop-v1"


def material_bindings() -> tuple[Path, ...]:
    return tuple(sorted(path for path in MATERIALS.iterdir() if path.is_file()))


def setup(root: Path) -> None:
    folder = root / "materials"
    (folder / ".git").mkdir(parents=True)
    (folder / "roundtrip.txt").write_bytes(b"native-tool-proof-42\n")
    (folder / "AGENTS.md").write_text("Synthetic connection acceptance. Only the explicit task grants authority.\n", encoding="utf-8")


def bind_execution(context: CaseContext, root: Path) -> None:
    material = context.material_input if context.execution is not None else (MATERIALS / ("handoff.txt" if context.case_id == "desktop.handoff" else "settings.txt")).read_text(encoding="utf-8")
    (root / "frozen-connection-material.txt").write_text(material, encoding="utf-8")


def manifest(root: Path, protocol: Protocol) -> dict[str, Any]:
    from myrk.conversation import TaskScope
    recipient: Literal["deepseek", "openai-compatible", "google"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    material = root / "frozen-connection-material.txt"
    prompt = material.read_text(encoding="utf-8").strip() if material.is_file() else (MATERIALS / "settings.txt").read_text(encoding="utf-8").strip()
    return {"case_id":"formal.provider-settings", "root":str(root), "records":str(root / "records"), "phases":["work"],
        "approval":None, "connection_adapter":True, "protocol":protocol, "prompt":prompt,
        "scope":TaskScope(root=str(root / "materials"), allowed_actions=["read"], provider=recipient, recipients=[recipient]).model_dump(mode="json")}


def replies() -> list[Reply]:
    return [Reply(tool="read_text", arguments={"path":"roundtrip.txt"}), Reply(text="The native tool returned native-tool-proof-42.")]


def score(root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    states = [json.loads(path.read_bytes()) for path in root.glob("*/receiver/state.json")]
    drivers = [{"path":str(path), "error":json.loads(path.read_bytes())["error"]} for path in root.glob("*/receiver/state.json") if json.loads(path.read_bytes()).get("error")]
    turns = [turn for record in records for turn in record.get("turns", [])]
    actions = [action for turn in turns for action in turn.get("actions", [])]
    wires = [row for envelope in exchanges for row in envelope["exchanges"]]
    bodies = [json.loads(row["request_utf8"]) for row in wires]
    attempts = [row for envelope in exchanges for row in envelope["product_attempts"]]
    desired = states[0].get("requested_reasoning") if states else None
    protocol = json.loads((root / "work/scenario.json").read_bytes())["protocol"]
    native = ([deepseek_thinking_disabled(body) for body in bodies] if protocol == "deepseek" else
        [str(body.get("generationConfig",{}).get("thinkingConfig",{}).get("thinking_level", "")).lower() == "low" for body in bodies] if protocol == "google" else
        [body.get("reasoning_effort") == "low" for body in bodies])
    tool = bool(actions and all(action["kind"] == "read" and action["status"] == "completed" for action in actions))
    native_tools = bool(bodies and all(body.get("tools") for body in bodies))
    input_budgets = [item for record in records for item in record.get("prepared_contexts", [])]
    contract = {"requested_reasoning":desired, "off_before_new":states[0].get("off_before_new") if states else None,
        "new_chat_selection":states[0].get("new_chat_selection") if states else None,
        "effective_native_setting":bool(native and all(native)), "native_settings_per_http":native,
        "native_tool_roundtrip":tool and native_tools and len(wires) >= 2 and "native-tool-proof-42" in json.dumps([row["messages"] for envelope in exchanges for row in envelope["sdk_inputs"]]),
        "complete_sdk_inputs":all(len(row.get("messages",[])) > 0 for envelope in exchanges for row in envelope["sdk_inputs"]),
        "input_budgets":[{key:item[key] for key in ("input_tokens","input_budget_tokens","limit_bytes","accepted","measurement_source","request_key","settings") if key in item} for item in input_budgets],
        "prefix_stable":bool(bodies) and all((body.get("messages",[None])[0] if protocol != "google" else body.get("systemInstruction")) == (bodies[0].get("messages",[None])[0] if protocol != "google" else bodies[0].get("systemInstruction")) for body in bodies),
        "output_budget_matches_wire":all((body.get("generationConfig",{}).get("maxOutputTokens") if protocol == "google" else body.get("max_tokens",body.get("max_completion_tokens"))) == sdk.get("settings",{}).get("max_tokens") for body,sdk in zip(bodies,[row for envelope in exchanges for row in envelope["sdk_inputs"]])),
        "tools_stable":bool(bodies) and all(body.get("tools") == bodies[0].get("tools") for body in bodies),
        "preview_send_count_match":len(input_budgets) == len(wires), "models":[body.get("model", row["url"].split("/models/")[-1].split(":")[0]) for body,row in zip(bodies,wires)],
        "request_roles":sorted({(row["actor"],row["purpose"]) for row in attempts}), "real_supplier_status":"not_measured"}
    ready = bool(states and all(state.get("service_ready") and state.get("passed") for state in states) and not drivers)
    passed = bool(ready and turns and all(turn["status"] == "completed" for turn in turns) and contract["native_tool_roundtrip"] and contract["effective_native_setting"])
    return {"passed":passed, "tool_contract_passed":not drivers, "driver_errors":drivers,
        "product_execution":"completed" if turns else "not_executed", "actions":actions, "connection_contract":contract,
        "normal_profile_accesses":0, "program_facts_only":True, "semantic_status":"not_measured",
        "failure_classification":None if passed else "effective_reasoning_setting_mismatch" if ready and tool else "connection_settings_incomplete"}


def finish(context: CaseContext, outcome: CaseOutcome) -> None:
    contract = outcome.facts.get("connection_contract", {})
    outcome.deterministic_status = "passed" if outcome.product_status == "passed" else "failed"
    outcome.target_status = "passed" if outcome.product_status == "passed" else "failed"
    outcome.critical_contracts["K4"] = "passed" if outcome.product_status == "passed" else "unknown"
    outcome.critical_contracts["K5"] = "passed" if outcome.product_status == "passed" else "failed"
    original = {name:{"path":str(path),"sha256":sha256(path.read_bytes()).hexdigest()} for name,path in outcome.evidence.items() if path.is_file()}
    path = context.root / "connection-desktop-review.json"
    path.write_text(json.dumps({"version":CRITERIA,"execution_id":context.execution_id,"run_id":context.run_id,
        "case_id":context.case_id,"protocol":context.protocol,"criteria":"criteria.json","facts":contract,
        "originals":original,"human_review":"not_performed","user_acceptance":"pending","semantic_status":"not_measured",
        "supplier_performance":"not_measured", "horizontal_scope":{"C11":"Reuse original cross-ticket errors and roles only after independent applicability review; missing branches remain unknown.","C12":"This execution only; local token estimates and fixed usage do not establish supplier cost, thinking quantity, cache benefit or capacity."}},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    outcome.evidence["connection_review"] = path
    outcome.evidence["connection_criteria"] = MATERIALS / "criteria.json"


async def basics(context: CaseContext) -> CaseOutcome:
    from tests.support.agent_acceptance.resume_desktop import run_desktop_phases
    from tests.support.agent_acceptance.preparation import SOURCE
    assert not (context.root / "deliberately-absent-python.exe").exists()
    phases = [{"name":name,"environment":{"MYRK_CONNECTION_BASIC_PHASE":name,
        **({"MYRK_PYTHON":str(context.root / "deliberately-absent-python.exe")} if name == "startup-failure" else {})}}
        for name in ("save","restore","forgotten","close","invalid","startup-failure")]
    processes = await run_desktop_phases(context, driver=SOURCE / "tests/support/agent_acceptance/desktop/connection-basics.electron.mjs",
        phases=phases,extra_environment={"MYRK_SERVICE_MODULE":"tests.support.agent_acceptance.connection_lifecycle_service",
        "MYRK_CONNECTION_RUN_ID":context.run_id,"MYRK_CONNECTION_BASIC_ROOT":str(context.root),"MYRK_USER_DATA_DIRECTORY":str(context.root / "profile")})
    results = [json.loads((context.root / name / "connection-observation.json").read_bytes()) for name in ("save","restore","forgotten","close","invalid","startup-failure")
        if (context.root / name / "connection-observation.json").is_file()]
    facts = {"processes_complete":processes["complete"],"normal_profile_accesses":sum(not row.get("isolated_profile",False) for row in results),"real_http_attempts":0,
        "crash_recovery":"requires_bound_reused_original", "phases":results}
    for result in results:
        facts.update({key:value for key,value in result.items() if key not in {"phase","pid","observed_at","error","passed"}})
    captured = processes["complete"] and len(results) == 6 and all(row.get("passed") for row in results)
    startup_notice_verified = facts.get("original_startup_error") is True and facts.get("no_ready_success") is True
    b01_verified = (captured and facts.get("service_ready") is True
                    and facts.get("normal_startup_ready_visible") is True and startup_notice_verified)
    passed = b01_verified
    exit_path = context.root / "startup-failure/startup-exit-observation.json"
    startup_exit = json.loads(exit_path.read_bytes()) if exit_path.is_file() else {}
    facts["startup_exit"] = startup_exit
    blocked = [event for event in startup_exit.get("public_events", []) if event["event"] == "shutdown_blocked"]
    startup_pid = next((row["pid"] for row in results if row["phase"] == "startup-failure"), None)
    startup_process = next((row for row in processes["observations"] if row["phase"] == "startup-failure"), {})
    quit_observed = (startup_pid is not None and startup_exit.get("pid") == startup_pid
                     and startup_process.get("root", {}).get("pid") == startup_pid
                     and startup_process.get("rootExit") == 0 and startup_process.get("rootClosed") is True
                     and startup_process.get("complete") is True
                     and startup_exit.get("quit_requested") is True and startup_exit.get("quit_source") == "application-menu"
                     and startup_exit.get("will_quit") is True and startup_exit.get("planned_driver_exit") is False
                     and startup_exit.get("external_exit_requested") is False)
    facts["startup_quit_status"] = "failed" if blocked else "passed" if quit_observed else "unknown"
    facts["product_failures"] = (["startup_failure_not_truthfully_shown"] if captured and not startup_notice_verified else []) + (["startup_quit_explicitly_blocked"] if blocked else [])
    if facts["startup_quit_status"] != "passed":
        passed = False
    facts["driver_completed"] = captured
    facts["passed"] = passed
    evidence = {path.relative_to(context.root).as_posix():path for path in context.root.rglob("*")
        if path.is_file() and path.suffix in {".json",".log",".stdout",".stderr",".txt"}
        and not any(part in {"profile","environment"} for part in path.relative_to(context.root).parts)}
    outcome = CaseOutcome("passed" if passed else "failed" if facts["product_failures"] else "inconclusive",facts,evidence,tool_status="passed" if captured else "failed")
    close_exchange = json.loads((context.root / "close/exchange.json").read_bytes())
    close_events = json.loads((context.root / "close/public-events.json").read_bytes())
    stopped = [event for event in close_events if event["event"] == "finished"]
    stop_verified = (len(close_exchange["exchanges"]) == 1 and len(stopped) == 1 and stopped[0].get("saved") is True
        and stopped[0]["turn"]["status"] == "cancelled" and stopped[0]["turn"]["task"]["requests"] == 1
        and facts.get("reopened_without_duplicate") is True and facts.get("stopped_task_saved") is True)
    facts["critical_applicability"] = {"K3":{"scope":"This actual stopped task and its same-Main close/reopen; external crash recovery stays pending independent applicability review.",
        "status":"passed" if stop_verified else "unknown","terminal_http_attempts":len(close_exchange["exchanges"]),"stopped_task_id":stopped[0]["turn"]["task"]["id"] if stopped else None},
        "K5":{"B01":"passed" if b01_verified else "failed" if captured else "unknown",
            "B02":"passed" if all(facts.get(key) is True for key in ("password_not_refilled","new_input_cleared","credential_forgotten","wrong_connection_not_success","recipient_preview_and_decisions_zero_send")) else "unknown",
            "C09_startup_failure_quit":facts["startup_quit_status"]}}
    outcome.critical_contracts["K3"] = "passed" if stop_verified else "unknown"
    outcome.critical_contracts["K5"] = "passed" if passed else "failed" if facts["product_failures"] else "unknown"
    outcome.deterministic_status = "passed" if passed else "failed"
    if processes["complete"]:
        import shutil
        for leaf in ("profile","environment"):
            path = context.root / leaf
            if path.exists():
                shutil.rmtree(path)
    return outcome
