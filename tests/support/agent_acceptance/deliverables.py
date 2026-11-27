"""Actual multi-file deliveries, identities and reopen facts; terminal HTTP alone is scripted."""
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any, Literal
from myrk.conversation import TaskScope
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, CaseRegistry, Protocol
from tests.support.agent_acceptance.delegation import returned_object
from tests.support.agent_acceptance.replay import Reply

MATERIALS = Path(__file__).resolve().parents[2] / "acceptance/materials/deliverables"
CRITERIA = "deliverables-v1"
SCENARIOS = {"formal.deliverables": ("deliverables", "deliverables.multi-file", 20)}
SCREENING = {"formal.deliverables": {"deepseek":{"scenario_id":"D18","request_cap":20,"criteria_version":CRITERIA},
    "google":{"scenario_id":"G05","request_cap":20,"criteria_version":CRITERIA}}}

PHASES = ("work", "reopen")


def material_bindings() -> tuple[Path, ...]:
    return tuple(sorted(path for path in MATERIALS.iterdir() if path.is_file()))


def setup(case_id: str, root: Path, protocol: Protocol) -> None:
    folder = root / "materials"
    (folder / ".git").mkdir(parents=True)
    (folder / "child").mkdir()
    for source, target in (("input.txt","input.txt"),("before.txt","existing.txt")):
        (folder / target).write_bytes((MATERIALS / source).read_bytes())


def bind_execution(context: CaseContext, root: Path) -> None:
    material = context.material_input if context.execution is not None else (MATERIALS / "task.txt").read_text(encoding="utf-8")
    (root / "frozen-deliverables-material.txt").write_text(material,encoding="utf-8")


def manifest(case_id: str, root: Path, protocol: Protocol) -> dict[str, Any]:
    recipient: Literal["deepseek","openai-compatible","google"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    prompt = (root / "frozen-deliverables-material.txt").read_text(encoding="utf-8") if (root / "frozen-deliverables-material.txt").exists() else (MATERIALS / "task.txt").read_text(encoding="utf-8")
    return {"case_id":case_id,"deliverables_adapter":True,"root":str(root),"records":str(root / "records"),
        "phases":list(PHASES),"prompt":prompt,"approval":None,"approval_mode":"request_approval",
        "scope":TaskScope(root=str(root / "materials"), allowed_actions=["read","edit","check"],provider=recipient,recipients=[recipient]).model_dump(mode="json")}


def extension(payload: dict[str,Any], name: str, arguments: dict[str,Any]) -> dict[str,Any]:
    values: list[Any] = [payload]
    descriptions=[]
    while values:
        value=values.pop(0)
        if isinstance(value,dict):
            if value.get("name")==name and value.get("status")=="ok" and "schema_version" in value: descriptions.append(value)
            values.extend(value.values())
        elif isinstance(value,list): values.extend(value)
        elif isinstance(value,str) and value.startswith("{"): values.append(json.loads(value))
    if not descriptions: raise ValueError("Missing actual extension description: " + name)
    return {"name":name,"schema_version":descriptions[-1]["schema_version"],"arguments":arguments}


def deliver(payload: dict[str,Any], latest: bool = False) -> dict[str,Any]:
    values: list[Any]=[payload]
    changes=[]
    while values:
        value=values.pop(0)
        if isinstance(value,dict): values.extend(value.values())
        elif isinstance(value,list): values.extend(value)
        elif isinstance(value,str) and "文件改动证据：" in value:
            evidence=json.loads(value.split("文件改动证据：",1)[1])
            changes.extend(evidence if isinstance(evidence,list) else [evidence])
    unique={row["id"]:row for row in changes}
    selected=list(unique.values())[-1:] if latest else list(unique.values())
    if not selected: raise ValueError("No actual file change was observed before delivery.")
    return extension(payload,"deliver_files",{"change_ids":[row["id"] for row in selected]})


def replies(case_id: str, root: Path, protocol: Protocol, phase: str = "work") -> list[Reply]:
    if phase == "reopen": return []
    request=manifest(case_id,root,protocol)
    child={**request["scope"],"root":str(root / "materials/child"),"allowed_actions":["read","edit"]}
    command="from pathlib import Path; Path('command.txt').write_bytes(b'command-output-4831'+bytes([10])); Path('temporary.txt').write_bytes(b'temporary-not-delivered'+bytes([10]))"
    return [Reply(tool="read_text",arguments={"path":"input.txt"}),
        Reply(tool="write_text",arguments={"path":"input.txt","content":(MATERIALS/"input.txt").read_text(encoding="utf-8")}),
        Reply(tool="create_text",arguments={"path":"new.txt","content":(MATERIALS/"new.txt").read_text(encoding="utf-8")}),
        Reply(tool="read_text",arguments={"path":"existing.txt"}),
        Reply(tool="edit_text",arguments={"path":"existing.txt","old_text":"old-7319","new_text":"first-8420"}),
        Reply(tool="describe_extension",arguments={"name":"run_command_with_outputs"}),
        Reply(tool="call_extension",arguments=lambda payload:extension(payload,"run_command_with_outputs",{"program":sys.executable,"arguments":["-B","-c",command],"output_paths":["command.txt"]})),
        Reply(tool="describe_extension",arguments={"name":"deliver_files"}),
        Reply(tool="call_extension",arguments=deliver),
        Reply(tool="call_extension",arguments=deliver),
        Reply(tool="edit_text",arguments={"path":"existing.txt","old_text":"first-8420","new_text":"final-9531"}),
        Reply(tool="call_extension",arguments=lambda payload:deliver(payload,True)),
        Reply(tool="delegate_work",arguments={"goal":"Create and explicitly deliver child.txt with child-output-2648 followed by one LF newline.","context":"Only child-local child.txt is authorized. Parent operations in this evidence do not enlarge your scope. User acceptance remains pending. Frozen task evidence: "+request["prompt"],"evidence_requirements":"Return actual change evidence and delivery identity; no user acceptance claim.","expected_files":["child/child.txt"],"scope":child}),
        Reply(tool="create_text",arguments={"path":"child.txt","content":"child-output-2648\n"}),
        Reply(tool="describe_extension",arguments={"name":"deliver_files"}),
        Reply(tool="call_extension",arguments=lambda payload:deliver(payload,True)),
        Reply(text="The child file has actual delivery evidence. Parent verification and user acceptance remain separate."),
        Reply(text="The actual new, modified, command and child files are explicitly delivered. Read input, unchanged write and temporary file are excluded. User acceptance remains pending.")]


def score(case_id: str, root: Path, records: list[dict[str,Any]], exchanges: list[dict[str,Any]]) -> dict[str,Any]:
    states=[json.loads(path.read_bytes()) for path in root.glob("*/receiver/state.json")]
    errors=[state["error"] for state in states if state.get("error")]
    owners=[json.loads(path.read_bytes()) for phase in PHASES if (path := root / phase / "owner/state.json").is_file()]
    accesses=[owner.get("normal_profile_accesses") for owner in owners]
    normal_profile_accesses=(sum(accesses) if len(owners)==len(PHASES)
        and all(type(value) is int and value>=0 for value in accesses)
        and all(type(owner.get("synthetic_profile")) is bool for owner in owners) else None)
    turns=[turn for record in records for turn in record.get("turns",[])]
    parents=[turn for turn in turns if turn.get("request_owner_id") is None]
    parent=parents[-1] if parents else {}
    actions={action["id"]:action for turn in turns for action in turn.get("actions",[])}
    artifacts=parent.get("artifacts",[])
    expected={"new.txt":(MATERIALS/"new.txt").read_bytes(),"existing.txt":(MATERIALS/"after.txt").read_bytes(),"command.txt":b"command-output-4831\n","child.txt":b"child-output-2648\n"}
    current={name:[item for item in artifacts if item["name"]==name][-1:] for name in expected}
    actual=bool(len(artifacts)==5 and all(current.values()) and all(Path(rows[0]["path"]).is_file() and Path(rows[0]["path"]).read_bytes()==expected[name] and rows[0]["delivered_sha256"]==sha256(expected[name]).hexdigest() for name,rows in current.items()))
    modifications=[item for item in artifacts if item["name"]=="existing.txt"]
    before=(MATERIALS/"before.txt").read_bytes()
    first=(MATERIALS/"first.txt").read_bytes()
    changes=[actions[item["source_action_id"]]["file_changes"] for item in modifications]
    diff=bool(len(modifications)==2 and modifications[0]["id"]!=modifications[1]["id"] and len(changes)==2
        and modifications[0]["delivered_sha256"]==sha256(first).hexdigest() and modifications[1]["delivered_sha256"]==sha256((MATERIALS/"after.txt").read_bytes()).hexdigest()
        and Path(changes[0][0]["backup_path"]).read_bytes()==before and Path(changes[1][0]["backup_path"]).read_bytes()==first
        and changes[0][0]["before_sha256"]==sha256(before).hexdigest() and changes[1][0]["before_sha256"]==sha256(first).hexdigest())
    deliveries=[json.loads(action["output"])["artifacts"] for action in actions.values() if action["kind"]=="deliver" and action["status"]=="completed"]
    duplicate=bool(len(deliveries)>=4 and [item["id"] for item in deliveries[0]]==[item["id"] for item in deliveries[1]])
    no_extra=bool({item["name"] for item in artifacts}==set(expected) and all(not action["file_changes"] for action in actions.values() if action["target"].endswith("input.txt")))
    original=json.loads((root/"deliverables-original-state.json").read_bytes()) if (root/"deliverables-original-state.json").is_file() else {}
    command_actions=[action for action in actions.values() if action["kind"]=="command"]
    command_proof=bool(len(command_actions)==1 and command_actions[0]["status"]==command_actions[0]["effect"]=="completed"
        and command_actions[0]["exit_code"]==0 and command_actions[0]["cleanup_complete"] and command_actions[0]["tool_name"]=="run_command_with_outputs"
        and command_actions[0]["prepared_parameters"]["output_paths"]==[str(root/"materials/command.txt")]
        and (root/"materials/temporary.txt").read_bytes()==b"temporary-not-delivered\n"
        and (root/"materials/input.txt").read_bytes()==(MATERIALS/"input.txt").read_bytes())
    actual=actual and command_proof
    works=parent.get("delegations",[])
    child_turns=[turn for turn in turns if turn.get("request_owner_id") is not None]
    child_art=current.get("child.txt",[])
    identity=bool(len(works)==len(child_turns)==len(child_art)==1 and child_turns[0]["request_owner_id"]==works[0]["id"]
        and child_turns[0]["task"]["id"]==parent["task"]["id"] and child_art[0]["delegation_id"]==works[0]["id"]
        and child_art[0]["source_record_path"]==works[0]["record_path"] and all(item["task_id"]==parent["task"]["id"] and (item["delegation_id"] is not None or item["source_record_path"]==original.get("path")) for item in artifacts)
        and all(actions[item["source_action_id"]]["status"]==actions[item["source_action_id"]]["effect"]=="completed" for item in artifacts))
    wires=[row for envelope in exchanges for row in envelope["exchanges"]]
    observations=[state.get("deliverables",{}) for state in states]
    observed={key:value for row in observations for key,value in row.items()}
    facts={"new_modified_command_and_child_bytes":actual,"independent_before_after":diff,"same_change_same_identity":duplicate,
        "no_read_temp_or_noop_deliveries":no_extra,"actual_parent_child_identity":identity,"request_attempts":len(wires),
        "actors":[row["request_identity"] for row in wires],"user_acceptance":"pending","controlled_user_acceptance":observed.get("controlled_user_acceptance"),
        **observed}
    required=("new_modified_command_and_child_bytes","independent_before_after","same_change_same_identity","no_read_temp_or_noop_deliveries","actual_parent_child_identity","reopen_identity_and_zero_send","cross_chat_isolation","changed_and_missing_honest")
    passed=bool(all(facts.get(key) is True for key in required) and len(wires)<=20 and not errors and all(state.get("passed") for state in states))
    return {"passed":passed,"tool_contract_passed":not errors,"driver_errors":errors,"normal_profile_accesses":normal_profile_accesses,"deliverables":facts,"product_execution":"completed" if parent.get("status")=="completed" else "incomplete","semantic_status":"not_measured","program_facts_only":True}


def finish(context: CaseContext, outcome: CaseOutcome) -> None:
    facts=outcome.facts["deliverables"]
    outcome.deterministic_status="passed" if outcome.product_status=="passed" else "failed"
    outcome.critical_contracts={"K1":"passed" if facts.get("actual_parent_child_identity") and facts.get("cross_chat_isolation") else "unknown",
        "K2":"not_applicable","K3":"passed" if facts.get("reopen_identity_and_zero_send") else "unknown",
        "K4":"passed" if outcome.product_status=="passed" else "unknown","K5":"passed" if facts.get("reopen_identity_and_zero_send") and facts.get("changed_and_missing_honest") else "unknown"}
    facts["critical_applicability"]={"K2":{"status":"not_applicable","reason":"Actual actions stayed inside the declared task and child scopes; no approval/review was requested or claimed."},
        "K3":{"status":outcome.critical_contracts["K3"],"trigger":"Actual saved Conversation reopened in a fresh Main; original task/artifact identity and zero terminal requests are observed."},
        "K5":{"status":outcome.critical_contracts["K5"],"scope":"This execution's actual launch, save/reopen and read-only text preview; no claim for other connection/Office contracts."}}
    path=context.root/"deliverables-review.json"
    path.write_text(json.dumps({"criteria_version":CRITERIA,"execution_id":context.execution_id,"facts":facts,"human_review":"not_performed","semantic_status":"not_measured","user_acceptance":"pending","attachment_kind":"artifact_evidence_not_an_output_type"},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    outcome.evidence["deliverables_review"]=path
    outcome.evidence["deliverables_criteria"]=MATERIALS/"criteria.json"


async def command_failure(context: CaseContext) -> CaseOutcome:
    from myrk.conversation import Conversation, Task
    from tests.support.agent_acceptance.actions import record_outcome
    folder=context.root/"materials"
    folder.mkdir()
    stopped=context.case_id=="deliverables.command-stop"
    command="from pathlib import Path; Path('partial.txt').write_bytes(b'partial-before-failure-5713'+bytes([10])); raise SystemExit(7)"
    if stopped: command=command.replace("raise SystemExit(7)","import time; time.sleep(60)")
    session=context.session([Reply(tool="describe_extension",arguments={"name":"run_command_with_outputs"}),
        Reply(tool="call_extension",arguments=lambda payload:extension(payload,"run_command_with_outputs",{"program":sys.executable,"arguments":["-B","-c",command],"output_paths":["partial.txt"]})),
        *([] if stopped else [Reply(status=400)])])
    conversation=Conversation(context.root/"conversation.json")
    task=Task(goal="Run the declared command and reliably deliver its actual output; preserve any failure and partial effects.",provider_config=session.config,
        scope=TaskScope(root=str(folder),allowed_actions=["read","edit","check"],provider="deepseek",recipients=["deepseek"]))
    stop_observation=None
    async with session.model() as model:
        if stopped:
            import asyncio
            from time import monotonic
            pending=asyncio.create_task(conversation.ask(task.goal,document=None,scope=task.scope,task=task,model=model,cancellation_token=context.cancellation_token))
            async with asyncio.timeout(30):
                while not (folder/"partial.txt").is_file(): await asyncio.sleep(.01)
            stop_observation={"requests_at_stop":len(session.exchanges),"partial_sha256":sha256((folder/"partial.txt").read_bytes()).hexdigest(),
                "action_ids_at_stop":[action.id for action in conversation.turns[-1].actions],"accepted_at_monotonic":monotonic()}
            context.cancellation_token.cancel()
            turn=await pending
            stop_observation["requests_after_stop"]=len(session.exchanges)
            stop_observation["action_ids_after_stop"]=[action.id for action in turn.actions]
            (context.root/"stop-observation.json").write_text(json.dumps(stop_observation,indent=2)+"\n",encoding="utf-8")
        else:
            turn=await conversation.ask(task.goal,document=None,scope=task.scope,task=task,model=model)
    action=next(action for action in turn.actions if action.kind=="command")
    partial=folder/"partial.txt"
    reopened=Conversation.open(conversation.path)
    observed=bool(partial.is_file() and partial.read_bytes()==b"partial-before-failure-5713\n" and action.status==("cancelled" if stopped else "failed")
        and (stopped or action.exit_code==7) and action.cleanup_complete and not action.file_changes and not turn.artifacts and turn.status==("cancelled" if stopped else "failed")
        and turn.delivery_status=="not_delivered" and reopened.turns[-1].artifacts==[] and reopened.turns[-1].task is not None
        and reopened.turns[-1].task.id==task.id and reopened.turns[-1].task.total_requests==task.total_requests and len(session.exchanges)==(2 if stopped else 3) and (not stopped or stop_observation is not None
            and stop_observation["action_ids_at_stop"]==stop_observation["action_ids_after_stop"]))
    facts={"partial_bytes_observed":observed,"reliable_artifacts":[item.model_dump(mode="json") for item in turn.artifacts],
        "actual_turn_status":turn.status,"actual_action_status":action.status,"actual_exit_code":action.exit_code,"goal_status":"failed",
        "checks_passed":turn.checks_passed,"delivery_status":turn.delivery_status,"user_acceptance":"pending","reopen_without_execution":observed,
        **({"stop_accepted_after_partial_write":observed,**stop_observation} if stop_observation is not None else {}),
        "control_kind":"actual command failure followed by terminal Mock HTTP400; not a formal successful execution"}
    outcome=record_outcome(context,session,conversation,task,turn,False,facts={"deliverables":facts},evidence={"partial_file":partial,**({"stop_observation":context.root/"stop-observation.json"} if stopped else {})})
    outcome.tool_status="passed" if observed else "failed"
    outcome.deterministic_status="failed"
    outcome.target_status="failed"
    outcome.critical_contracts={"K1":"passed" if observed else "unknown","K2":"not_applicable","K3":"passed" if observed else "unknown","K4":"passed" if observed and not turn.answer else "unknown","K5":"unknown"}
    facts["critical_applicability"]={"K2":{"status":"not_applicable","reason":"Actual declared command stayed in the authorized root and no decision was requested."},
        "K3":{"status":outcome.critical_contracts["K3"],"trigger":"Actual failed task saved and reopened; same task/usage, no action or request restarted."},
        "K5":{"status":"unknown","reason":"This control uses public Conversation and actual save; desktop BasicChain is measured by formal.deliverables, not inherited here."}}
    return outcome


def register(registry: CaseRegistry) -> None:
    from tests.support.agent_acceptance.core import AcceptanceCase
    registry.register(AcceptanceCase("deliverables.unverified","deliverables",("deepseek",),unverified,
        owner_ticket="17",expectation="planned_incomplete_delivery",bindings=material_bindings()))
    for name in ("deliverables.command-failure","deliverables.command-stop"):
        registry.register(AcceptanceCase(name,"deliverables",("deepseek",),command_failure,
            owner_ticket="17",expectation="planned_incomplete_delivery",bindings=material_bindings()))


async def unverified(context: CaseContext) -> CaseOutcome:
    from myrk.conversation import Conversation, Task
    from tests.support.agent_acceptance.actions import record_outcome
    folder=context.root/"materials"
    folder.mkdir()
    session=context.session([Reply(tool="create_text",arguments={"path":"unverified.txt","content":"created-before-change-4926\n"}),
        Reply(tool="describe_extension",arguments={"name":"deliver_files"}),Reply(tool="call_extension",arguments=deliver),
        Reply(text="The source changed before delivery verification. Reliable delivery is unfinished; no user acceptance is claimed.")])
    conversation=Conversation(context.root/"conversation.json")
    task=Task(goal="Create and reliably deliver the exact file; preserve failures and do not claim acceptance.",provider_config=session.config,
        scope=TaskScope(root=str(folder),allowed_actions=["read","edit"],provider="deepseek",recipients=["deepseek"]))
    observations=[]
    def observe(event: Any) -> None:
        if event.action is not None and event.action.kind=="deliver" and event.action.status=="started":
            source=folder/"unverified.txt"
            before=source.read_bytes()
            source.write_bytes(b"independent-external-change-6037\n")
            observations.append({"action_id":event.action.id,"before_sha256":sha256(before).hexdigest(),"after_sha256":sha256(source.read_bytes()).hexdigest()})
    async with session.model() as model:
        turn=await conversation.ask(task.goal,document=None,scope=task.scope,task=task,model=model,on_update=observe)
    source=folder/"unverified.txt"
    creates=[action for action in turn.actions if action.kind=="create"]
    deliveries=[action for action in turn.actions if action.kind=="deliver"]
    reopened=Conversation.open(conversation.path)
    observed=bool(len(observations)==len(creates)==len(deliveries)==1 and creates[0].status=="completed"
        and creates[0].file_changes[0].after_sha256==sha256(b"created-before-change-4926\n").hexdigest()
        and deliveries[0].status=="failed" and source.read_bytes()==b"independent-external-change-6037\n"
        and turn.status=="completed" and not turn.artifacts and not reopened.turns[-1].artifacts and reopened.turns[-1].task is not None
        and reopened.turns[-1].task.id==task.id and turn.delivery_status!="accepted" and len(session.exchanges)==4)
    facts={"source_changed_before_delivery":observed,"reliable_artifacts":[],"actual_turn_status":turn.status,
        "required_delivery_status":"failed","delivery_status":turn.delivery_status,"checks_passed":turn.checks_passed,
        "user_acceptance":"pending","external_changes":observations,"goal_status":"failed"}
    outcome=record_outcome(context,session,conversation,task,turn,False,facts={"deliverables":facts},evidence={"unverified_source":source})
    outcome.tool_status="passed" if observed else "failed"
    outcome.target_status="failed"
    outcome.deterministic_status="failed"
    outcome.critical_contracts={"K1":"passed" if observed else "unknown","K2":"not_applicable","K3":"passed" if observed else "unknown",
        "K4":"passed" if observed and "unfinished" in turn.answer else "unknown","K5":"unknown"}
    facts["critical_applicability"]={"K2":{"status":"not_applicable","reason":"No approval or independent review was requested or claimed."},
        "K3":{"status":outcome.critical_contracts["K3"],"trigger":"Actual saved file-change and failed-delivery identities survive Conversation.open with no new execution."},
        "K5":{"status":"unknown","reason":"This control does not run a Main/BasicChain; no blanket desktop pass is inherited."}}
    return outcome
