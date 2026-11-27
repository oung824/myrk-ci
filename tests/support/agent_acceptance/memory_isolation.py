"""Synthetic memory lifecycle materials for the existing formal Main path."""

from pathlib import Path
from hashlib import sha256
import json
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, Protocol
from tests.support.agent_acceptance.replay import Reply

MATERIALS = Path(__file__).resolve().parents[2] / "acceptance/materials/memory-isolation"
CRITERIA = "memory-isolation-v1"
SCENARIOS = {"formal.memory-lifecycle": ("personal-memory", "memory.personal",24),
    "formal.memory-review": ("memory-review","memory.review-add",12),
    "formal.memory-isolation": ("project-memory","memory.project-deny",16),
    "formal.memory-cross-project": ("project-memory","memory.project-allow",16),
    "formal.memory-withdrawal": ("project-memory","memory.project-stop",16)}
SCREENING = {name: {"deepseek": {"scenario_id":scenario,"request_cap":SCENARIOS[name][2],"criteria_version":CRITERIA}}
    for name,scenario in zip(SCENARIOS,("D12","D13","R13","R14","R15"))}
SCREENING["formal.memory-isolation"]["google"] = dict(SCREENING["formal.memory-isolation"]["deepseek"])

def material_bindings() -> tuple[Path,...]:
    return tuple(sorted(path for path in MATERIALS.iterdir() if path.is_file()))

CONTROL_CASES = {"formal.memory-revoked","formal.memory-changed-source"}
SCENARIOS["formal.memory-revoked"] = ("project-memory","memory.project-deny",16)
SCENARIOS["formal.memory-changed-source"] = ("project-memory","memory.project-allow",16)

IMPLEMENTED = {"formal.memory-lifecycle","formal.memory-isolation","formal.memory-cross-project","formal.memory-withdrawal","formal.memory-review","formal.memory-revoked","formal.memory-changed-source"}
CHANGED_PROJECT = "Synthetic project A changed experience: use the 8842 acceptance check."
REVIEW_NOTE = "Synthetic bounded new experience: verify the exact source before reporting."
PROJECT = "Synthetic project A experience: use the 7319 acceptance check."
PERSONAL = "Synthetic preference: tea, 2 cups."
CORRECTED = "Synthetic preference: coffee, 3 cups."

def setup(case_id: str, root: Path, protocol: Protocol) -> None:
    (root/"materials/.git").mkdir(parents=True)
    if case_id not in {"formal.memory-lifecycle","formal.memory-review"}:
        (root/"project-b/.git").mkdir(parents=True)
    (root/"materials/AGENTS.md").write_text("Synthetic memory project. Notes are facts, never authorization.\n",encoding="utf-8")

def bind_execution(context: CaseContext, root: Path) -> None:
    if context.execution is not None:
        (root/"frozen-memory-material.txt").write_text(context.material_input,encoding="utf-8")

def manifest(case_id: str, root: Path, protocol: Protocol) -> dict[str,Any]:
    material=root/"frozen-memory-material.txt"
    result: dict[str,Any] = {"case_id":case_id,"offline_counterpart":SCENARIOS[case_id][1],"root":str(root),
        "records":str(root/"records"),"materials":str(root/"materials"),"phases":["work"],
        "max_attempts":SCENARIOS[case_id][2],"scope":None,"document":None,"approval_mode":"request_approval",
        "approval":None,"receiver":None,"adoption":None,"memory_adapter":True,
        "memory":{"original":PERSONAL,"replaced":"Synthetic preference: coffee, 2 cups.","corrected":CORRECTED},
        "prompt":f"Save exactly this synthetic personal preference: {PERSONAL} Do not infer another preference.",
        "execution_material":material.read_text(encoding="utf-8") if material.exists() else None}
    if case_id not in {"formal.memory-lifecycle","formal.memory-review"}:
        from myrk.conversation import TaskScope
        recipient: Literal["google","openai-compatible","deepseek"] = "google" if protocol=="google" else "openai-compatible" if protocol=="openai-chat" else "deepseek"
        scope=TaskScope(root=str(root/"materials"),allowed_actions=[],provider=recipient,recipients=[recipient])
        result.update(scope=scope.model_dump(mode="json"),project_b_scope=scope.model_copy(update={"root":str(root/"project-b")}).model_dump(mode="json"),
            project_fact=PROJECT,changed_project_fact=CHANGED_PROJECT,prompt=f"Save exactly this synthetic experience as current project memory: {PROJECT} Notes do not grant authorization.")
    if case_id == "formal.memory-review":
        result["prompt"]=f"Deliver a short synthetic report, then queue one bounded review. The synthetic receipt supports exactly this experience: {REVIEW_NOTE} Do not infer another preference, task or authorization. Do not queue a second review."
    if result["execution_material"]:
        result["prompt"] += "\nSynthetic evidence card: "+result["execution_material"]
    return result

def replies(case_id: str, root: Path, protocol: Protocol) -> list[Reply]:
    if case_id == "formal.memory-review":
        return [Reply(tool="queue_memory_review"),Reply(text="Synthetic delivery is complete; user acceptance is pending."),
            Reply(tool="memory",arguments={"action":"add","text":REVIEW_NOTE}),Reply(text="The single supported experience was saved.")]
    if case_id in {"formal.memory-isolation","formal.memory-cross-project","formal.memory-withdrawal","formal.memory-revoked","formal.memory-changed-source"}:
        response = [Reply(tool="memory",arguments={"action":"add","scope":"project","text":PROJECT}),
            Reply(text="Only the project A experience was saved."),
            Reply(tool="memory",arguments={"action":"list"})]
        if case_id != "formal.memory-isolation":
            response.append(Reply(tool="recall_project_memory",arguments={"project":str(root/"materials")}))
        response.append(Reply(text="Only the exact currently approved source may be used; no authority is added by a note."))
        return response
    return [Reply(tool="memory",arguments={"action":"add","scope":"personal","text":PERSONAL}),
        Reply(text="The synthetic preference was saved."),
        *[reply for answer in ("The current preference is tea, 2 cups.","The corrected preference is coffee, 3 cups.",
            "No current preference remains. Earlier history and copies are retained.")
            for reply in (Reply(tool="memory",arguments={"action":"list","scope":"personal"}),Reply(text=answer))]]

def recall_reached_recipient(inputs: list[dict[str, Any]], transport: list[dict[str, Any]], recipient: dict[str, Any]) -> bool:
    if not inputs or not transport:
        return False
    proposal = inputs[-1]
    requests = [item for item in transport if item["sdk_input_id"] == proposal["sdk_input_id"]
        and item["request_identity"] == proposal["request_identity"]]
    if len(requests) != 1:
        return False
    request = requests[0]
    if not request["url"].startswith(recipient["base_url"].rstrip("/") + "/"):
        return False
    returns = [part for message in proposal["messages"] for part in message["parts"]
        if part.get("part_kind") == "tool-return" and part.get("tool_name") == "recall_project_memory"]
    if len(returns) != 1 or returns[0]["content"] != PROJECT:
        return False
    call_id = returns[0]["tool_call_id"]
    body = json.loads(request["request_utf8"])
    if recipient["protocol"] == "google":
        model = unquote(urlsplit(request["url"]).path).partition("/models/")[2].partition(":")[0]
        sent = [response["response"]["return_value"] for item in body["contents"] for part in item["parts"]
            if (response := part.get("functionResponse")) and response["name"] == "recall_project_memory" and response["id"] == call_id]
    else:
        model = body.get("model")
        sent = [item["content"] for item in body["messages"] if item["role"] == "tool" and item["tool_call_id"] == call_id]
    return model == recipient["model"] and sent == [PROJECT]


def score(case_id: str, root: Path, records: list[dict[str,Any]], exchanges: list[dict[str,Any]]) -> dict[str,Any]:
    states=[json.loads(path.read_bytes()) for path in root.glob("*/receiver/state.json")]
    driver=states[0]
    windows=driver.get("memory_windows",[])
    sdk=[row for envelope in exchanges for row in envelope["sdk_inputs"]]
    wire=[row for envelope in exchanges for row in envelope["exchanges"]]
    turns=[turn for record in records for turn in record.get("turns",[])]
    actions=[action for turn in turns for action in turn["actions"]]
    attempts=[row for envelope in exchanges for row in envelope["product_attempts"]]
    normal_accesses=sum(json.loads(path.read_bytes())["normal_profile_accesses"] for path in root.glob("*/owner/state.json"))
    notes=root/"records/memories/facts.json"
    current=json.loads(notes.read_bytes())["facts"] if notes.exists() else None
    if case_id == "formal.memory-review":
        reviews=[item for item in sdk if item["request_identity"]["actor"]=="memory_review"]
        actors=[item["actor"] for item in attempts]
        source=bool(reviews) and all(REVIEW_NOTE in json.dumps(item,ensure_ascii=False) for item in reviews)
        only=bool(reviews) and all([tool["name"] for tool in item["parameters"]["function_tools"]]==["memory"] for item in reviews)
        once=bool(turns) and all(turn["task"]["memory_reviews"]==1 for turn in turns if turn.get("task"))
        repeated=any("已经进行过一次有限整理" in json.dumps(item,ensure_ascii=False) for item in sdk)
        preserved=any(turn["answer"]==driver.get("original_delivery") for turn in turns) and driver.get("original_delivery")=="Synthetic delivery is complete; user acceptance is pending."
        contract={"memory_reviews":1 if once else None,"review_tools_memory_only":only,"repeat_review_refused":None,"no_second_review":len(reviews)==2,
            "request_actors":actors,"original_delivery_preserved":preserved,"source_bound":source,"current_facts":current,
            "bounded_parent_requests":len(attempts)<=12}
        passed=bool(driver.get("passed") and current and len(current)==1 and current[0]["text"]==REVIEW_NOTE and current[0]["scope"]=="personal"
            and actors==["main","main","memory_review","memory_review"] and source and only and once and len(reviews)==2 and preserved and len(attempts)<=12)
        return {"passed":passed,"memory_contract":contract,"actions":actions,"normal_profile_accesses":normal_accesses,"program_facts_only":True,
            "request_roles":sorted({(row["actor"],row["purpose"]) for row in attempts}),"driver_errors":[driver["error"]] if driver.get("error") else []}
    if case_id in {"formal.memory-isolation","formal.memory-cross-project","formal.memory-withdrawal","formal.memory-revoked","formal.memory-changed-source"}:
        window=driver.get("project_b_window",{})
        inputs=sdk[window.get("sdk_start",len(sdk)):]
        transport=wire[window.get("http_start",len(wire)):]
        sdk_text=json.dumps(inputs,ensure_ascii=False)
        wire_text=json.dumps(transport,ensure_ascii=False)
        sdk_isolated=bool(inputs) and PROJECT not in sdk_text and CHANGED_PROJECT not in sdk_text
        wire_isolated=bool(transport) and PROJECT not in wire_text and CHANGED_PROJECT not in wire_text
        recalls=[action for action in actions if action["kind"]=="memory_recall"]
        decisions=[item for item in driver.get("approval_decisions",[]) if item["action"]["kind"]=="memory_recall"]
        initial=bool(inputs and transport) and PROJECT not in json.dumps(inputs[:2],ensure_ascii=False) and PROJECT not in json.dumps(transport[:2],ensure_ascii=False)
        cross=case_id=="formal.memory-cross-project"
        changed=case_id=="formal.memory-changed-source"
        stopped=case_id=="formal.memory-withdrawal"
        withdrawn=case_id=="formal.memory-revoked"
        stop=driver.get("stop_observation")
        stop_event=driver.get("stop_event")
        no_after=(bool(len(sdk)==stop["sdk_count"] and set(action["id"] for action in actions if action["task_id"]==stop_event["task_id"])<=set(stop["action_ids"])
            and len(recalls)==1 and recalls[0]["status"]=="cancelled" and not recalls[0]["output"])
            if stop and stop_event else None)
        source=str((root/"materials").resolve())
        recipient_project=str((root/"project-b").resolve())
        bound=False
        approved=False
        seen=False
        stale_blocked=False
        if len(decisions)==len(recalls)==1:
            recall=recalls[0]
            decision=decisions[0]
            presented=decision["action"]
            pending=next((entry["action"] for record in records for entry in record.get("action_evidence",[])
                if entry["phase"]=="prepared" and entry["action"]["id"]==recall["id"]),{})
            task=next((turn["task"] for turn in turns if (turn.get("task") or {}).get("id")==recall["task_id"]),{})
            provider=task.get("provider_config",{})
            recipient={key:provider.get(key) for key in ("protocol","base_url","model")}
            prepared=pending.get("prepared_parameters",{})
            preview=presented.get("approval_preview")
            bound=bool(presented.get("approval_settings",{}).get("reviewer")=="user" and all(recipient.values())
                and presented["status"]==pending.get("status")=="awaiting_approval"
                and presented["target"]==pending.get("target")==recall["target"]==source
                and decision["task_id"]==presented["task_id"]==pending.get("task_id")==recall["task_id"]
                and presented["id"]==pending.get("id")==recall["id"]
                and presented["approval_id"]==pending.get("approval_id")==recall["approval_id"]
                and all(pending.get(key) and pending[key]==recall.get(key) for key in ("prepared_action_sha256","approval_context_sha256"))
                and presented.get("prepared_parameters")==prepared==recall.get("prepared_parameters")
                and preview==decision["snapshot_preview"]==PROJECT and prepared.get("content_sha256")==sha256(preview.encode("utf-8")).hexdigest()
                and "approval_preview" not in pending and "approval_preview" not in recall
                and PROJECT not in json.dumps(pending,ensure_ascii=False)
                and prepared.get("source_project")==source and prepared.get("recipient_project")==recipient_project
                and (task.get("scope") or {}).get("root")==recipient_project
                and all(prepared.get("recipient",{}).get(key)==value for key,value in recipient.items()))
            seen=bool(bound and recall_reached_recipient(inputs,transport,recipient))
            approved=bool(bound and decision["approved"] and recall["status"]==recall["effect"]=="completed"
                and recall["approval_basis"]=="user" and recall["output"]==PROJECT and seen)
            stale_blocked=bool(bound and decision["approved"] and recall["status"]=="conflict" and recall["effect"]=="not_started"
                and not recall["output"] and sdk_isolated and wire_isolated)
        change=driver.get("source_change",{})
        source_changed=bool(changed and change and len(recalls)==1 and change["approval_id"]==recalls[0]["approval_id"]
            and change["before_sha256"]!=change["after_sha256"]
            and all((root/f"source-{phase}.json").is_file()
                and sha256((root/f"source-{phase}.json").read_bytes()).hexdigest()==change[f"{phase}_sha256"] for phase in ("before","after")))
        stale_used=bool(source_changed and len(recalls)==1 and recalls[0]["status"]=="completed" and recalls[0]["output"]==CHANGED_PROJECT
            and CHANGED_PROJECT in sdk_text and CHANGED_PROJECT in wire_text
            and len(decisions)==1 and decisions[0]["approved"])
        contract={"project_b_sdk_isolated":sdk_isolated,"project_b_wire_isolated":wire_isolated,"initial_b_isolated":initial,
            "cross_project_effect":recalls[0]["status"] if recalls else "not_requested",
            "withdrawal_received":driver.get("withdrawal_received") if withdrawn else None,
            "stop_accepted":bool(stop_event) if stopped else None,"no_request_or_action_after_stop":no_after if stopped else None,
            "approved_source_content_recipient_match":approved if cross else None,
            "approval_content_binding":"bound" if bound else "unknown",
            "pending_exposes_exact_content":bool(decisions) and decisions[0]["action"].get("approval_preview")==PROJECT,
            "source_content_changed_after_pending":source_changed if changed else None,"stale_approval_supplied_changed_source":stale_used if changed else None,
            "stale_approval_blocked":stale_blocked if changed else None,
            "source_change":driver.get("source_change"),"source_seen_after_approval":seen,
            "current_facts":current,"bounded_parent_requests":len(attempts)<=16}
        if cross:
            scenario_passed=initial and approved
        elif changed:
            scenario_passed=initial and source_changed and stale_blocked
        elif stopped:
            scenario_passed=sdk_isolated and wire_isolated and no_after
        elif withdrawn:
            scenario_passed=sdk_isolated and wire_isolated and driver.get("withdrawal_received") and len(recalls)==1 and recalls[0]["status"]=="denied"
        else:
            scenario_passed=sdk_isolated and wire_isolated and not recalls
        passed=bool(driver.get("passed") and scenario_passed and len(attempts)<=16 and current and len(current)==1
            and current[0]["text"]==(CHANGED_PROJECT if changed else PROJECT) and current[0]["project"]==source)
        return {"passed":passed,"controlled_product_failure_observed":stale_used if changed else False,"memory_contract":contract,"actions":actions,"request_roles":sorted({(row["actor"],row["purpose"]) for row in attempts}),
            "normal_profile_accesses":normal_accesses,"program_facts_only":True,"driver_errors":[driver["error"]] if driver.get("error") else []}
    expected=[PERSONAL,CORRECTED,""]
    actual=[]
    matched=len(windows)==3
    for window,expected_fact in zip(windows,expected):
        applicable=[fact["text"] for fact in window["memory_state"]["facts"] if fact["scope"]=="personal"]
        actual.append("\n".join(applicable))
        inputs=sdk[window["sdk_start"]:window["sdk_end"]]
        transport=wire[window["sdk_start"]:window["sdk_end"]]
        originals=[record for record in records if any((turn.get("task") or {}).get("id")==window["task_id"] for turn in record["turns"])]
        matched=matched and bool(inputs and transport and len(originals)==1)
        if originals:
            matched=matched and originals[0]["memory_contexts"]["personal"]==expected_fact
        if expected_fact:
            matched=matched and all(expected_fact in json.dumps(item,ensure_ascii=False) for item in inputs)
            matched=matched and all(expected_fact in item["request_utf8"] for item in transport)
        else:
            matched=matched and all(text not in json.dumps(inputs,ensure_ascii=False) and text not in json.dumps(transport,ensure_ascii=False) for text in (PERSONAL,CORRECTED))
    original=json.loads((root/"memory-original-history.json").read_bytes())
    seed_id=original["turns"][0]["task"]["id"]
    seeds=[record for record in records if any((turn.get("task") or {}).get("id")==seed_id for turn in record["turns"])]
    history=len(seeds)==1 and seeds[0]["messages"]==original["messages"] and seeds[0]["turns"]==original["turns"]
    removals=[action for action in actions if any(change["action"]=="remove" for change in action.get("memory_changes") or [])]
    limited=bool(removals) and all("只影响当前事实笔记" in action["output"] and "旧聊天、摘要、备份、导出和其他待审提议保留" in action["output"] for action in removals)
    contract={"fresh_chat_current_facts":actual,"current_facts":current,"original_history_preserved":history,
        "current_deletion_only":limited,"actual_sdk_and_wire_match":matched,"bounded_parent_requests":len(attempts)<=24}
    passed=bool(driver.get("passed") and not driver.get("error") and actual==expected and matched and current==[] and history and limited
        and len(attempts)<=24 and all(action["status"]=="completed" for action in actions))
    return {"passed":passed,"memory_contract":contract,"actions":actions,"request_roles":sorted({(row["actor"],row["purpose"]) for row in attempts}),
        "normal_profile_accesses":normal_accesses,"program_facts_only":True,"driver_errors":[driver["error"]] if driver.get("error") else []}

def finish(context: CaseContext, outcome: CaseOutcome) -> None:
    contract=outcome.facts["memory_contract"]
    standard=json.loads((MATERIALS/"criteria.json").read_bytes())
    outcome.facts.update(criteria_version=CRITERIA,output_standard={"version":CRITERIA,"dimensions":{key:{"description":text,"applicable":True} for key,text in standard["dimensions"].items()}},
        output_domain_criteria={"version":CRITERIA,"criteria":standard,"actual_facts":contract,"human_review":"not_performed"})
    outcome.deterministic_status="passed" if outcome.product_status=="passed" else "failed"
    outcome.critical_contracts.update(K1="unknown",K2="unknown",K3=("failed" if contract.get("no_request_or_action_after_stop") is False else "passed" if contract.get("no_request_or_action_after_stop") is True else "unknown") if context.case_id=="formal.memory-withdrawal" else "not_applicable",K4="unknown",K5="not_applicable")
    if context.case_id=="formal.memory-changed-source" and outcome.facts.get("controlled_product_failure_observed"):
        outcome.critical_contracts.update(K1="failed",K2="failed")
    path=context.root/"memory-domain-review.json"
    path.write_text(json.dumps({"criteria_version":CRITERIA,"execution":{"case_id":context.case_id,"execution_id":context.execution_id,
        **({"material_sha256":context.execution.material_sha256,"material_family_id":context.execution.material_family_id,"material_variant":context.execution.material_variant} if context.execution else {})},
        "actual_facts":contract,"human_review":"not_performed","semantics":"not_measured","user_acceptance":"pending",
        "originals":{label:{"path":str(original),"sha256":sha256(original.read_bytes()).hexdigest()} for label,original in outcome.evidence.items() if original.is_file()}},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    outcome.evidence["memory_review"]=path
    outcome.evidence["memory_standard"]=MATERIALS/"criteria.json"
