"""Bounded post-delivery memory review with original sources and saved effects."""
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal

import httpx2
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse

from myrk.conversation import Conversation, ConversationRecord, Task, TaskScope
from myrk.memory import MemoryRecord, MemoryStore
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig
from tests.support.agent_acceptance.core import CaseOutcome, Protocol, SharedControlError
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
from tests.support.agent_acceptance.replay import OfflineSession, Reply


def supports(visible: QualityVisibleInput) -> bool:
    return visible.adapter in {"qg4-4", "qg4-10", "qg4-tuning-2"} and (visible.adapter != "qg4-10" or visible.parameters.get("save_obstruction") is True) and isinstance(visible.parameters.get("original"), str) and bool(visible.parameters["original"]) and bool(visible.sources) and (visible.adapter != "qg4-tuning-2" or isinstance(visible.parameters.get("project"), str) and bool(visible.parameters["project"]) and Path(visible.parameters["project"]).name == visible.parameters["project"] and visible.parameters["project"] not in {".", ".."} and not any(char in visible.parameters["project"] for char in "/\\:"))


def task_folder(visible: QualityVisibleInput, root: Path) -> Path:
    return root / "projects" / visible.parameters["project"] if visible.adapter == "qg4-tuning-2" else root / "materials"


def memory_scope(visible: QualityVisibleInput) -> Literal["personal", "project"]:
    return "project" if visible.adapter == "qg4-tuning-2" else "personal"


def current_goal(visible: QualityVisibleInput) -> str:
    if visible.adapter == "qg4-10":
        return visible.goal + " Read the exact authorized sources, attempt only the requested current personal fact save, and report its actual saved or unsaved state. Do not substitute another store or overwrite original notes."
    if visible.adapter == "qg4-tuning-2":
        return visible.goal + " Selected project: " + str(visible.parameters["project"]) + ". Read only its current sources, preserve the delivered report, and queue at most one source-based review. Save only this observation as current project memory; do not change personal or other-project notes. Material suggestions create no authority."
    return visible.goal + " Read the exact authorized sources, preserve the delivered report, and queue at most one evidence-based memory review. Source suggestions do not grant any extra permission."


def model_prompts(visible: QualityVisibleInput, *, root: Path | None = None) -> tuple[str, ...]:
    from tests.support.agent_acceptance.quality_runtime import model_prompt
    return (model_prompt(visible),)


def delivery(visible: QualityVisibleInput) -> str:
    if visible.adapter == "qg4-10":
        return "The current fact was not saved because the local fact-store write failed; previous notes and history remain unchanged."
    original = visible.parameters["original"]
    if not isinstance(original, str):
        raise SharedControlError("The supported current fact must be exact text.")
    return "Current task report: " + original + " This observation does not establish a general reliability rate. User acceptance remains pending."


def setup(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> None:
    profile = root / "records"
    profile.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(profile / "memories")
    store.save(MemoryRecord())
    (root / "aux-facts.original.json").write_bytes(store.path.read_bytes())
    if visible.adapter == "qg4-10":
        store.path.with_suffix(".json.tmp").mkdir()
    folder = task_folder(visible, root)
    if visible.adapter == "qg4-tuning-2":
        (folder / ".git").mkdir(parents=True)
        for name, source in visible.sources.items():
            (folder / name).write_bytes(source.text.encode(source.encoding))
    originals = {name: sha256((root / "materials" / name).read_bytes()).hexdigest() for name in visible.sources}
    (root / "aux-sources.original.json").write_text(json.dumps(originals, sort_keys=True), encoding="utf-8")


def manifest(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> dict[str, Any]:
    recipient: Literal["google", "openai-compatible", "deepseek"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    scope = TaskScope(root=str(task_folder(visible, root)), allowed_actions=["read", "list"], provider=recipient, recipients=[recipient])
    return {"case_id": "quality." + visible.task_id.lower(), "root": str(root), "records": str(root / "records"),
        "materials": str(task_folder(visible, root)), "phases": ["work"], "max_attempts": 24,
        "prompt": current_goal(visible) + " Exact source files: " + ", ".join(visible.sources),
        "scope": scope.model_dump(mode="json"), "approval_mode": "request_approval", "approval": None,
        "document": None, "receiver": None, "adoption": None, "quality_group": "QG4",
        "quality_memory_aux": {"task_id": visible.task_id, "adapter": visible.adapter}}


def replies(visible: QualityVisibleInput, root: Path, protocol: Protocol, *, phase: str = "work") -> list[Reply]:
    if not supports(visible):
        raise SharedControlError("This exact auxiliary memory task is not implemented.")
    if visible.adapter == "qg4-10":
        return [*[Reply(tool="read_text", arguments={"path": name}) for name in visible.sources],
            Reply(tool="memory", arguments={"action": "add", "scope": memory_scope(visible), "text": visible.parameters["original"]}), Reply(text=delivery(visible))]
    return [*[Reply(tool="read_text", arguments={"path": name}) for name in visible.sources],
        Reply(tool="queue_memory_review"), Reply(text=delivery(visible)),
        Reply(tool="memory", arguments={"action": "add", "scope": memory_scope(visible), "text": visible.parameters["original"]}),
        Reply(text="The bounded review kept this task's supported observation only; it did not revise the delivered report.")]


class AuxiliaryMemoryMock:
    def __init__(self, visible: QualityVisibleInput, root: Path, protocol: Protocol, config: ProviderConfig, *, phase: str = "work") -> None:
        self.replies = replies(visible, root, protocol, phase=phase)
        self.offset = 0
        self.terminal = OfflineSession(protocol, [], config=config)

    def respond(self, request: httpx2.Request, sdk: dict[str, Any], purpose: str) -> httpx2.Response:
        if purpose != "work" or self.offset >= len(self.replies):
            raise SharedControlError("Auxiliary memory exceeded its declared finite native reply sequence.")
        self.terminal.replies.append(self.replies[self.offset])
        self.offset += 1
        return self.terminal.respond(request)


def terminal_mock(visible: QualityVisibleInput, protocol: Protocol, config: ProviderConfig, *, phase: str = "work") -> AuxiliaryMemoryMock:
    return AuxiliaryMemoryMock(visible, Path(), protocol, config, phase=phase)


def _saved_delivery(records: list[dict[str, Any]], turns: list[dict[str, Any]]) -> tuple[str | None, bool | None]:
    if len(records) != 1 or len(turns) != 1 or turns[0]["status"] != "completed" or not records[0].get("messages"):
        return None, None
    response = ModelMessagesTypeAdapter.validate_python(records[0]["messages"][-1:])[0]
    answer = turns[0]["answer"]
    if not isinstance(response, ModelResponse):
        return None, None
    preserved = bool(answer.strip() and answer == response.text and turns[0]["delivery_status"] == "awaiting_acceptance")
    return answer if preserved else None, preserved


def score(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]], *, observation: dict[str, Any] | None = None, request_budget: dict[str, Any] | None = None) -> dict[str, Any]:
    turns = [turn for record in records for turn in record.get("turns", [])]
    actions = list({action["id"]: action for turn in turns for action in turn.get("actions", [])}.values())
    sdk = [item for envelope in exchanges for item in envelope["sdk_inputs"]]
    wire = [item for envelope in exchanges for item in envelope["exchanges"]]
    attempts = [item for turn in turns for item in turn.get("request_attempts", [])]
    review_sdk = [item for item in sdk if item.get("request_identity", {}).get("actor") == "memory_review"]
    review_wire = [item for item in wire if item.get("request_identity", {}).get("actor") == "memory_review"]
    tools_only = bool(review_sdk) and all([tool["name"] for tool in item["parameters"]["function_tools"]] == ["memory"] for item in review_sdk)
    reviews = max((turn.get("task", {}).get("memory_reviews", 0) for turn in turns if turn.get("task")), default=0)
    actual = MemoryStore(root / "records/memories").read()
    texts = [fact.text for fact in actual.facts]
    originals = json.loads((root / "aux-sources.original.json").read_bytes())
    unchanged = all(sha256((root / "materials" / name).read_bytes()).hexdigest() == digest
        and (task_folder(visible, root) == root / "materials" or sha256((task_folder(visible, root) / name).read_bytes()).hexdigest() == digest)
        for name, digest in originals.items())
    reads = {Path(action["prepared_parameters"]["path"]).resolve() for action in actions if action["kind"] == "read" and action["status"] == "completed"}
    source_read = all((task_folder(visible, root) / name).resolve() in reads for name in visible.sources)
    answer, delivered = _saved_delivery(records, turns)
    report_sha256 = sha256(answer.encode()).hexdigest() if answer is not None else None
    if visible.adapter == "qg4-10":
        memory_actions = [action for action in actions if action["kind"] == "memory"]
        failure = memory_actions[0] if len(memory_actions) == 1 else None
        actual_failure = bool(failure and failure["status"] == "failed" and failure["effect"] == "unknown"
            and failure["approval_basis"] == "task_scope" and failure["denial_source"] is None
            and (failure.get("error") or "").startswith("事实笔记保存失败："))
        original = (root / "aux-facts.original.json").read_bytes()
        fact_unchanged = (root / "records/memories/facts.json").read_bytes() == original
        obstruction = (root / "records/memories/facts.json.tmp").is_dir()
        complete = bool(turns) and all(turn["status"] == "completed" for turn in turns)
        changes = (failure.get("memory_changes") or []) if failure else []
        authorized = all(action["kind"] in {"read", "memory"} for action in actions) and len(changes) == 1 and all(
            change["scope"] == memory_scope(visible) and change["action"] == "add" and change["text"] == visible.parameters["original"]
            for change in changes)
        passed = complete and actual_failure and fact_unchanged and obstruction and delivered is True and source_read and unchanged and authorized and not texts and reviews == 0 and len(attempts) == len(wire)
        return {"passed": passed, "tool_contract_passed": complete and bool(sdk and wire), "program_facts_only": True, "actions": actions,
            "quality_memory_contract": {"task_id": visible.task_id, "actual_save_failure": actual_failure, "fact_original_unchanged": fact_unchanged,
                "failed_effect": failure["effect"] if failure else None, "denial_source": failure["denial_source"] if failure else None,
                "current_texts": texts, "delivery_preserved": delivered, "delivered_report_sha256": report_sha256, "actual_temporary_directory_obstruction": obstruction,
                "original_fact_sha256": sha256(original).hexdigest(), "actual_source_read": source_read, "source_originals_unchanged": unchanged},
            "critical_contracts": {"K1": "passed" if authorized and source_read else "failed" if actions and not authorized else "unknown",
                "K2": "not_applicable" if actual_failure else "unknown", "K3": "not_applicable", "K4": "failed" if delivered is False or not fact_unchanged else "unknown",
                "K5": "passed" if unchanged and source_read else "failed" if not unchanged else "unknown"},
            "critical_contract_applicability": {"K1": "Only the explicit current source reads and one requested personal memory addition are authorized; no replacement store or overwrite is permitted.",
                "K2": "No decision is requested: this fresh store has approve_writes=false and the actual task_scope addition fails before applying.",
                "K3": "This task has no stop, save/reopen or replay branch; the fact-store save fails within a normally saved conversation.",
                "K4": "Verify the actual failed action, unknown effect, original fact bytes and delivered report identity. Human review of actual_status verifies whether the report states the unsaved result truthfully.",
                "K5": "Actual reads preserve every frozen source byte; original fact-store bytes remain exact."}}
    separate = bool(turns and turns[0].get("memory_review_messages")) and all("按本任务资料进行一次有限复盘。" not in json.dumps(record.get("messages", []), ensure_ascii=False) for record in records)
    forbidden = [action for action in actions if action["kind"] not in {"read", "memory"}]
    applied = [action for action in actions if action["kind"] == "memory"]
    authorized = not forbidden and all(action["status"] == "completed" and all(change["scope"] == memory_scope(visible) and change["action"] == "add" and change["text"] == visible.parameters["original"] for change in action.get("memory_changes") or []) for action in applied)
    complete = bool(turns) and all(turn["status"] == "completed" for turn in turns)
    source_bound = bool(review_sdk) and all(visible.parameters["original"] in json.dumps(item, ensure_ascii=False) for item in review_sdk)
    review_sdk_ids = {item.get("sdk_input_id"): item.get("request_identity") for item in review_sdk}
    review_sdk_complete = bool(review_wire and review_sdk_ids and None not in review_sdk_ids and len(review_sdk_ids) == len(review_sdk)
        and all(item.get("sdk_input_id") in review_sdk_ids and review_sdk_ids[item["sdk_input_id"]] == item["request_identity"] for item in review_wire))
    review_report_bound = (all(any(isinstance(message, ModelResponse) and message.text == answer
        for message in ModelMessagesTypeAdapter.validate_python(item["messages"])) for item in review_sdk)
        if answer is not None and review_sdk_complete else None)
    namespace = all(fact.scope == memory_scope(visible) and (fact.scope != "project" or fact.project == str(task_folder(visible, root))) for fact in actual.facts)
    review_attempts = [item for item in attempts if item["actor"] == "memory_review"]
    tasks = {turn["task"]["id"]: turn["task"] for turn in turns if turn.get("task")}
    operations = sum(task["memory_reviews"] for task in tasks.values())
    mutations = [change for action in applied for change in action.get("memory_changes") or []]
    review_messages = turns[0].get("memory_review_messages", []) if len(turns) == 1 else []
    review_completed = (review_messages[-1]["kind"] == "response" and any(part["part_kind"] == "text" and part["content"]
        for part in review_messages[-1]["parts"])) if review_messages else None
    identities = {(item["number"], item["actor"], item["purpose"]) for item in attempts}
    request_evidence = bool(attempts and sdk and wire and review_attempts and review_sdk_complete) and len(attempts) == len(wire) and all(
        (item.get("request_identity", {}).get("number"), item.get("request_identity", {}).get("actor"), item.get("request_identity", {}).get("purpose")) in identities
        for item in [*sdk, *wire])
    budget_checks: list[bool | None] = []
    for task in tasks.values():
        used = task.get("requests")
        cap = task.get("request_limit")
        budget_checks.append(None if used is None else used <= cap if cap is not None else None if request_budget is None else True)
        budget_checks.append(task["total_requests"] >= used >= len(attempts) if used is not None and len(tasks) == 1 else None)
    if not tasks:
        budget_checks.append(None)
    if request_budget is not None:
        reserved = request_budget["requests"]
        cap = min(request_budget["frozen_request_cap"], request_budget["approved_request_cap"])
        budget_checks.extend([request_budget["origin"] == "validated_controller_ledger", request_budget["reserved_requests"] == len(reserved), len(reserved) <= cap,
            request_budget["case_id"] == "quality." + visible.task_id.lower(),
            all(item["execution_id"] == request_budget["execution_id"] and item["case_id"] == request_budget["case_id"]
                and item["protocol"] == request_budget["protocol"] for item in reserved)])
        budget_checks.append(all(item["task_id"] in tasks and (item["product_number"], item["actor"], item["purpose"]) in identities
            for item in reserved if item["task_id"] is not None))
        budget_checks.append(True if request_budget["scope"] == "execution" else
            True if request_budget["scope"] == "task" and all(item["task_id"] is not None for item in reserved) else None)
    budget_status = "failed" if False in budget_checks else "passed" if budget_checks and all(item is True for item in budget_checks) and request_evidence else "unknown"
    bounded_status = ("failed" if budget_status == "failed" or operations > 1 or len(mutations) > 1 or review_completed is False else
        "passed" if budget_status == "passed" and operations == 1 and len(mutations) == 1 and review_completed is True else "unknown")
    passed = complete and bounded_status == "passed" and source_bound and review_report_bound is True and namespace and tools_only and texts == [visible.parameters["original"]] and source_read and unchanged and delivered is True and separate and authorized
    return {"passed": passed, "tool_contract_passed": complete and bool(sdk and wire), "program_facts_only": True, "actions": actions,
        "quality_memory_contract": {"task_id": visible.task_id, "memory_reviews": reviews, "review_tools_are_memory_only": tools_only,
            "delivery_preserved": delivered, "delivered_report_sha256": report_sha256, "review_report_bound": review_report_bound,
            "review_sdk_complete": review_sdk_complete,
            "review_history_separate": separate, "current_texts": texts,
            "actual_source_read": source_read, "source_originals_unchanged": unchanged, "request_actors": [item["actor"] for item in attempts], "review_source_bound": source_bound, "namespace_preserved": namespace, "review_request_count": len(review_attempts), "memory_review_operations": operations,
            "memory_mutation_count": len(mutations), "review_completed": review_completed, "bounded_review_status": bounded_status,
            "request_budget_status": budget_status, "controller_request_budget": request_budget,
            "saved_task_budgets": [{"task_id": task["id"], "request_limit": task.get("request_limit"), "requests": task.get("requests"), "total_requests": task.get("total_requests")} for task in tasks.values()]},
        "critical_contracts": {"K1": "failed" if not authorized or not namespace or budget_status == "failed" or operations > 1 or len(mutations) > 1 or review_sdk and not tools_only else "passed" if source_read and budget_status == "passed" else "unknown",
            "K2": "not_applicable" if complete and applied and all(action["approval_basis"] == "task_scope" and not action["memory_deferred"] for action in applied) else "unknown",
            "K3": "not_applicable", "K4": "failed" if delivered is False or review_report_bound is False or complete and texts != [visible.parameters["original"]] else "unknown",
            "K5": "passed" if unchanged and source_read else "failed" if not unchanged else "unknown"},
        "critical_contract_applicability": {"K1": "The task authorizes exact source reads and one bounded addition to the selected memory scope only; review tools must be memory-only.",
            "K2": "No user approval or independent action-review decision is requested; this fresh fact store has approve_writes=false and only an additive bounded review.",
            "K3": "This task has no stop, save/reopen, interruption or replay branch.",
            "K4": "Bind the unchanged delivered report to the actual review input and verify the current saved fact. The human review checks the report's content separately.",
            "K5": "The actual read actions must preserve every frozen source byte."}}


async def exercise_memory(reference: Any, task_id: str, protocol: Protocol, *, root: Path) -> CaseOutcome:
    from uuid import uuid4
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.formal import provider_config
    from tests.support.agent_acceptance.frozen import source_material_path
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest as canonical_manifest
    packet = read_quality_packet(reference)
    if packet.provenance != "synthetic" or packet.intended_use != "implementation_regression":
        raise ValueError("Auxiliary exercise cannot execute or qualify an unseen formal packet.")
    registry = builtin_registry(quality_packet=reference)
    case_id = "quality." + task_id.lower()
    if case_id not in registry.cases or not registry.cases[case_id].formal_live:
        raise ValueError("The exact canonical auxiliary memory task is not implemented.")
    root.mkdir(parents=True, exist_ok=False)
    visible = bind_packet(reference, task_id, root, protocol=protocol)
    request = canonical_manifest(case_id, root, protocol)
    scope = TaskScope.model_validate(request["scope"])
    approval = ApprovalSettings.model_validate(request["approval"])
    session = OfflineSession(protocol, replies(visible, root, protocol), config=provider_config(protocol))
    conversation = Conversation(root / "records/work.json")
    task = Task(goal=request["prompt"], scope=scope, approval=approval, request_limit=24, provider_config=session.config)
    async with session.model() as model:
        await conversation.ask(task.goal, document=None, task=task, scope=scope, model=model)
    record = ConversationRecord.model_validate_json(conversation.path.read_bytes())
    attempts = [{"task_id": turn.task.id, **attempt.model_dump(mode="json")} for turn in record.turns if turn.task for attempt in turn.request_attempts]
    exchange = session.save(root / "exchange.json", attempts=attempts, run_id=uuid4().hex, case_id=case_id, task_id=task.id)
    facts = score(visible, root, [record.model_dump(mode="json")], [json.loads(exchange.read_bytes())])
    facts.update(real_http_attempts=0, observation_layer="conversation-sdk-executor", formal_execution=False, independent_quality_sample_increment=0, actual_human_review="not_performed")
    evidence = {"conversation": conversation.path, "exchange": exchange, "facts": root / "records/memories/facts.json"}
    originals = root / "review-context"
    originals.mkdir()
    selected = next(item for item in packet.tasks if item.task_id == task_id)
    for label, source in (("input", selected.input_path), ("gold", selected.gold_path), ("freeze", reference.path)):
        target = originals / (label + ".original.json")
        target.write_bytes(source_material_path(source).read_bytes())
        evidence["quality." + label] = target
    outcome = CaseOutcome("passed" if facts["passed"] else "failed", facts, evidence, critical_contracts=dict(facts["critical_contracts"]))
    outcome.facts["output_domain_criteria"] = {"criteria": {kind: row.model_dump(mode="json") for kind, row in selected.domain_standards.items()}, "human_review": "not_performed"}
    return outcome
