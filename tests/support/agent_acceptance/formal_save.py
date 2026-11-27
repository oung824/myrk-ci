"""One current offline formal-send save failure, with shared strict observation rules."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from myrk.actions import Action
from myrk.conversation import Turn
from tests.support.agent_acceptance.core import CaseContext, SharedControlError
from tests.support.agent_acceptance.preparation import LiveAuthorization, PreparedBundle
from tests.support.agent_acceptance.reporting import environment_facts

REFERENCE = Path(__file__).resolve().parents[2] / "acceptance/materials/formal_save/reference.json"
RULE = "current-formal-send-google-save-failure-v1"
EXPECTATION = "success_or_current_offline_formal_google_send_save_failure"
ISSUE = ".scratch/acceptance-save-failure-diagnosis-20261113/issues/02-formal-google-send.md"
FILES = ("work/receiver/state.json", "work/receiver/events.json", "work/receiver/exchange.json", "receiver.json",
    "work/receiver/transport.json", "work/processes.json", "work/scenario.json", "work/owner/state.json", "work/relay/state.json", "materials/payload.txt")


@dataclass(frozen=True)
class FormalSaveObservation:
    case_id: str
    protocol: str
    mode: str
    run_id: str
    facts: dict[str, Any]
    version: dict[str, Any]
    current_version: dict[str, Any]
    current_sources: dict[str, str]
    directory: Path
    files: dict[str, Any]
    record_name: str
    approval_name: str
    prepared: dict[str, Any]
    ledger: dict[str, Any]
    grant: dict[str, Any]


def digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def current_sources(source: Path) -> dict[str, str]:
    reference = json.loads(REFERENCE.read_bytes())
    return {name: sha256((source / name).read_bytes()).hexdigest() for name in reference["source_pins"]}


def known_failure(value: dict[str, Any] | None) -> bool:
    return value is not None and all(value.get(key) == expected for key, expected in {
        "rule": RULE, "case_id": "formal.send", "protocol": "google", "mode": "offline",
        "product_status": "failed", "tool_status": "passed", "expectation_status": "review_required",
        "historical_relation": "unknown", "root_cause": "unknown", "product_fix_claimed": False,
        "missing_underlying_exception": True, "missing_reliable_record_bytes": True}.items())


def match(observation: FormalSaveObservation, reference: dict[str, Any]) -> dict[str, Any] | None:
    """Recognize the complete observed symptom using facts, with no file or model operations."""
    if (observation.case_id, observation.protocol, observation.mode) != ("formal.send", "google", "offline"):
        return None
    bindings = observation.version
    if (bindings["python"] != reference["python"] or observation.current_version["python"] != reference["python"]
            or bindings["packages"] != reference["packages"] or observation.current_version["packages"] != reference["packages"]
            or any(bindings["source_files"].get(name) != checksum or observation.current_sources.get(name) != checksum
                for name, checksum in reference["source_pins"].items())):
        return None
    directory, facts = observation.directory, observation.facts
    records = [observation.record_name]
    values = observation.files
    record, approval = values[observation.record_name], values[observation.approval_name]
    state, events = values["work/receiver/state.json"], values["work/receiver/events.json"]
    exchange, receiver = values["work/receiver/exchange.json"], values["receiver.json"]
    transport, processes = values["work/receiver/transport.json"], values["work/processes.json"]
    prepared, ledger, scenario = observation.prepared, observation.ledger, values["work/scenario.json"]
    owner, relay, payload = values["work/owner/state.json"], values["work/relay/state.json"], values["materials/payload.txt"]
    if transport["control_error"] is not None or ledger["stopped"]:
        raise SharedControlError("Current formal-save run has a shared control stop.")
    fingerprint = {key: prepared[key] for key in ("root", "source_root", "plan", "files", "execution", "environment")}
    expected_grant = {"authorization_id": "offline-qualification-" + observation.run_id, "binding": prepared["binding"],
        "total_limit": 10, "case_limits": {"formal.send": 10}, "transport": "mock", "connection_profile": None}
    if observation.grant != expected_grant:
        return None
    grant = observation.grant
    if (digest(fingerprint) != prepared["binding"] or ledger["binding"] != prepared["binding"]
            or ledger["authorization_binding"] != digest(grant)
            or len(ledger["attempts"]) > grant["total_limit"]
            or any(item["number"] != number or item["authorization_id"] != grant["authorization_id"]
                or (item["case_id"], item["protocol"]) != ("formal.send", "google")
                for number, item in enumerate(ledger["attempts"], 1))):
        raise SharedControlError("Current formal-save authorization ledger is inconsistent.")
    if len(record["turns"]) != 1:
        return None
    turn = record["turns"][0]
    if (turn["status"] != "limited" or len(record["save_failures"]) != 2 or len(facts["driver_errors"]) != 1
            or not isinstance(state["error"], str) or len(ledger["attempts"]) != 2):
        return None
    if ledger["attempts"] != [{"number": number, "authorization_id": grant["authorization_id"],
            "case_id": "formal.send", "protocol": "google", "actor": "main", "purpose": "work",
            "delegation_id": None, "product_number": number} for number in (1, 2)]:
        raise SharedControlError("Current formal-save request ownership differs from its candidate ledger.")
    if (prepared["execution"]["formal.send"]["compiled_sources"] != reference["compiled_sources"]
            or prepared["environment"]["packages"] != reference["packages"]
            or prepared["sending_enabled"] is not False or prepared["plan"] != {"cases": ["formal.send"], "protocols": ["google"], "mode": "prepare-live"}
            or any(prepared["files"].get(name) != value for name, value in reference["source_pins"].items())
            or state["runtime_versions"] != reference["runtime_versions"]):
        return None
    if len(turn["actions"]) != 2:
        return None
    read_action, send = turn["actions"]
    endpoint = urlsplit(receiver["url"])
    if (turn["answer"] != "" or turn["error"] != record["save_failures"][0]
            or (read_action["kind"], read_action["status"], read_action["effect"], read_action["target"]) != ("read", "completed", "completed", "payload.txt")
            or (send["kind"], send["status"], send["effect"], send["approval_basis"]) != ("send", "cancelled", "not_started", "pending")
            or send["target"] != receiver["url"] or receiver["received"] != [] or receiver["unexpected_requests"] != [] or receiver["closed"] is not True
            or (endpoint.scheme, endpoint.hostname, endpoint.path, endpoint.query, endpoint.fragment, endpoint.username) != ("http", "127.0.0.1", "/receive", "", "", None)
            or endpoint.port is None or sha256(payload).hexdigest() != reference["payload_sha256"]
            or scenario["receiver"]["payload_sha256"] != reference["payload_sha256"] or scenario["case_id"] != "formal.send"
            or scenario["fault_plan"] is not None or scenario["max_attempts"] != 10
            or any(action["task_id"] != turn["task"]["id"] for action in turn["actions"])
            or turn["task"]["scope"] != scenario["scope"] or turn["task"]["requests"] != 2 or turn["task"]["total_requests"] != 2 or turn["task"]["review_requests"] != 0):
        return None
    reliable_path = str(Path(scenario["records"]) / Path(records[0]).name)
    times = []
    for notice in record["save_failures"]:
        matched = re.fullmatch(r"(.+)：进展未能保存；最后可靠保存位置：" + re.escape(reliable_path) + r"；时间：(.+)。", notice)
        if matched is None or matched[2] != send["started_at"]:
            return None
        times.append(datetime.fromisoformat(matched[1]))
    if not (datetime.fromisoformat(read_action["finished_at"]) < datetime.fromisoformat(send["started_at"]) < times[0] < times[1]
            == datetime.fromisoformat(send["finished_at"]) < datetime.fromisoformat(record["last_saved_at"])):
        return None
    if (approval["approved"] is not True or approval["action"]["id"] != send["id"] or approval["task_id"] != turn["task"]["id"]
            or approval["approval_id"] != send["approval_id"] or approval["action"]["status"] != "awaiting_approval"
            or approval["action"]["effect"] != "not_started" or approval["action"]["approval_basis"] != "pending"
            or (directory / "work/receiver" / approval["record"]).resolve() != (directory / records[0]).resolve()
            or not re.fullmatch(r"[0-9a-f]{64}", approval["record_sha256_at_observation"])
            or state["approval_decisions"] != [approval] or state["passed"] is not False or state["normal_exit"] is not True or state["synthetic_profile"] is not True):
        return None
    if send["prepared_parameters"] != {"path": str(Path(scenario["materials"]) / "payload.txt"),
            "url": receiver["url"], "payload_sha256": reference["payload_sha256"]}:
        return None
    terminal = Action.model_validate(send)
    approved = Action.model_validate(approval["action"])
    expected_approval = terminal.model_copy(update={"status": "awaiting_approval", "finished_at": None, "error": None})
    internal_fields = {"tool_call_id", "prepared_parameters", "approval_context_sha256", "prepared_action_sha256"}
    public_approval = Action.model_validate(approval["event"]["action"])
    if (approved != expected_approval or approval["event"]["event"] != "action"
            or approval["event"]["path"] != reliable_path or approval["event"]["task_id"] != terminal.task_id
            or public_approval.model_dump(mode="json", exclude=internal_fields) != approved.model_dump(mode="json", exclude=internal_fields)
            or read_action["prepared_request"] != 1 or send["prepared_request"] != 2
            or terminal.error != "工作已停止；已发生的影响和输出保留。"
            or terminal.impact != f"新增资料接收方：{receiver['url']}；发送文件：{send['prepared_parameters']['path']}；内容：{payload.decode()}。"):
        return None
    approval_positions = [number for number, event in enumerate(events) if event == approval["event"]]
    failures = [number for number, event in enumerate(events) if event.get("event") == "status" and event.get("text") == turn["error"]]
    errors = [(number, event) for number, event in enumerate(events) if event.get("event") == "error"]
    finished = [(number, event) for number, event in enumerate(events) if event.get("event") == "finished"]
    public_fields = {"actions": {"__all__": {"tool_call_id", "prepared_parameters", "approval_context_sha256", "prepared_action_sha256"}}}
    if (len(approval_positions) != 1 or len(failures) != 1 or len(errors) != 1 or len(finished) != 1
            or not approval_positions[0] < failures[0] < errors[0][0] < finished[0][0]
            or errors[0][1] != {"event": "error", "message": "这份答复不对应当前待批准动作，未执行。", "path": reliable_path, "task_id": turn["task"]["id"]}
            or Turn.model_validate(finished[0][1]["turn"]).model_dump(mode="json", exclude=public_fields)
                != Turn.model_validate(turn).model_dump(mode="json", exclude=public_fields)):
        return None
    driver_error = state["error"].replace(turn["error"], "<turn-error>").replace(prepared["root"].replace("\\", "/"), "<prepared-root>")
    if (driver_error != reference["driver_error"] or facts["driver_errors"] != [{"path": str(Path(scenario["root"]) / "work/receiver/state.json"), "error": state["error"]}]
            or facts["compiled_sources_match"] is not True or facts["processes_complete"] is not True or facts["phases"] != [{"phase": "work", "exit_code": 0}]
            or facts["normal_profile_accesses"] != 0 or facts["real_http_attempts"] != 0 or facts["mock_http_attempts"] != 2
            or facts["unexpected_effect_action_ids"] != [] or facts["unrelated_failure_action_ids"] != [send["id"]]
            or facts["actions"] != turn["actions"] or facts["driver_completed"] is not False):
        return None
    if (processes["complete"] is not True or processes["root_exit"] != {"code": 0, "signal": None}
            or len(processes["processes"]) != 7 or len({row["pid"] for row in processes["processes"]}) != 7
            or any(row["state"] != "exited" or row["ownership"] != "owned" or row["completed"] is not True or row["exit_code"] != 0
                or row["errors"] != [] or row["reasons"] != [] or row["identity"] != row["expected"]
                or row["identity"] != row["direct_spawn"]["identity"] or row["pid"] != row["identity"]["pid"] for row in processes["processes"])
            or owner["normal_profile_accesses"] != 0 or owner["synthetic_profile"] is not True or owner["normal_exit"] is not True
            or relay["child_exit"] != 0 or relay["child_signal"] is not None or relay["delivered_once"] is not True):
        return None
    if (transport != {"transport_kind": "MockTransport", "mock_http_attempts": 2, "real_http_attempts": 0,
            "blocked_unplanned_http_attempts": 0, "control_error": None, "planned_local_transport_failures": 0}
            or exchange["run_id"] != observation.run_id or exchange["case_id"] != "formal.send" or exchange["protocol"] != "google"
            or exchange["task_id"] != turn["task"]["id"]
            or exchange["product_attempts"] != [{"task_id": turn["task"]["id"], **attempt} for attempt in turn["request_attempts"]]
            or len(exchange["sdk_inputs"]) != 2 or len(exchange["exchanges"]) != 2):
        return None
    sdk = {item["sdk_input_id"]: item for item in exchange["sdk_inputs"]}
    if len(sdk) != 2:
        return None
    for number, (request, attempt) in enumerate(zip(exchange["exchanges"], turn["request_attempts"], strict=True), 1):
        identity = {"number": number, "actor": "main", "purpose": "work", "delegation_id": None}
        captured = sdk.get(request["sdk_input_id"])
        if (captured is None or captured["request_identity"] != identity or request["request_identity"] != identity
                or any(attempt[key] != value for key, value in identity.items()) or attempt["status"] != "completed" or attempt["error"] is not None
                or request["status"] != 200 or request.get("error") is not None
                or attempt["observation"]["wire"]["body_sha256"] != sha256(request["request_utf8"].encode()).hexdigest()
                or request["received_utf8"].replace(receiver["url"], "<receiver>") != reference["received_utf8"][number - 1]):
            return None
    second = sdk[exchange["exchanges"][1]["sdk_input_id"]]["messages"]
    returns = [part for message in second for part in message["parts"] if part["part_kind"] == "tool-return"]
    calls = [part for message in second for part in message["parts"] if part["part_kind"] == "tool-call"]
    if (len(returns) != 1 or returns[0]["tool_name"] != "read_text" or returns[0]["outcome"] != "success"
            or returns[0]["content"] != "[payload.txt:1] " + payload.decode() or len(calls) != 1
            or calls[0]["tool_name"] != "read_text" or calls[0]["args"] != {"path": "payload.txt"}
            or calls[0]["tool_call_id"] != returns[0]["tool_call_id"] or returns[0]["tool_call_id"] != read_action["tool_call_id"]):
        return None
    return {"issue": ISSUE, "case_id": observation.case_id, "protocol": observation.protocol, "mode": observation.mode, "rule": reference["rule"], "product_status": "failed", "tool_status": "passed", "expectation_status": "review_required",
        "historical_relation": "unknown", "root_cause": "unknown", "product_fix_claimed": False,
        "missing_underlying_exception": True, "missing_reliable_record_bytes": True,
        "original_facts_sha256": digest(facts), "driver_error": state["error"]}


def _guarded(operation: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
    try:
        return operation()
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        raise SharedControlError(f"Current formal-save evidence is unreadable or malformed: {type(error).__name__}") from error


def recognize(root: Path, *, source: Path) -> dict[str, Any] | None:
    """Regression adapter for published saved originals; never writes their verdict."""
    return _guarded(lambda: _saved(root, source=source))


def _saved(root: Path, *, source: Path) -> dict[str, Any] | None:
    original = (root / "result.json").read_bytes()
    result = json.loads(original)
    if result["mode"] != "offline" or len(result["cases"]) != 1:
        return None
    case = result["cases"][0]
    if (case["case_id"], case["protocol"]) != ("formal.send", "google"):
        return None
    directory = root / "formal.send/google"
    index = json.loads((directory / "evidence-index.json").read_bytes())
    if (index["run_id"], index["case_id"], index["protocol"]) != (result["run_id"], "formal.send", "google"):
        raise SharedControlError("Current formal-save original index has different ownership.")

    def read(name: str) -> Any:
        path = (directory / name).resolve()
        link = index["links"][name]
        if (not path.is_relative_to(directory.resolve()) or (directory / link["path"]).resolve() != path
                or (root / case["evidence"][name]).resolve() != path):
            raise SharedControlError("Current formal-save original path binding changed.")
        body = path.read_bytes()
        if len(body) != link["bytes"] or sha256(body).hexdigest() != link["sha256"]:
            raise SharedControlError("Current formal-save original bytes changed.")
        return body if name == "materials/payload.txt" else json.loads(body)

    records = [name for name in index["links"] if name.startswith("records/") and name.count("/") == 1 and name.endswith(".json")]
    approvals = [name for name in index["links"] if name.startswith("work/receiver/approval-") and name.endswith(".json")]
    if len(records) != 1 or len(approvals) != 1:
        return None
    prepared, ledger = read("prepared.json"), read("ledger.json")
    grant = {"authorization_id": "offline-qualification-" + result["run_id"], "binding": prepared["binding"],
        "total_limit": 10, "case_limits": {"formal.send": 10}, "transport": "mock", "connection_profile": None}
    snapshot = FormalSaveObservation(case["case_id"], case["protocol"], result["mode"], result["run_id"], case["facts"], result["version"],
        environment_facts(), current_sources(source), directory, {name: read(name) for name in (*FILES, records[0], approvals[0])},
        records[0], approvals[0], prepared, ledger, grant)
    matched = match(snapshot, json.loads(REFERENCE.read_bytes()))
    if matched is not None:
        matched["original_result_sha256"] = sha256(original).hexdigest()
    return matched


def legacy_runtime_inputs(bundle: PreparedBundle, grant: LiveAuthorization) -> tuple[dict[str, Any], dict[str, Any]]:
    """Serialize the actual frozen fields for the legacy observation contract."""
    excluded_fields = {"total_microusd", "case_microusd"} if grant.total_microusd is None and not grant.case_microusd else set()
    if grant.parent_budget_id is None:
        excluded_fields.add("parent_budget_id")
    return bundle.model_dump(mode="json", exclude_unset=True), grant.model_dump(mode="json", exclude=excluded_fields)


def recognize_runtime(context: CaseContext, *, runtime: Path, bundle: PreparedBundle, grant: LiveAuthorization,
                      ledger_path: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]],
                      facts: dict[str, Any], source: Path) -> dict[str, Any] | None:
    """Observe existing verified runtime artifacts before CaseOutcome and publication."""
    if (context.case_id, context.protocol, context.mode, grant.transport) != ("formal.send", "google", "offline", "mock"):
        return None
    return _guarded(lambda: _runtime(context, runtime=runtime, bundle=bundle, grant=grant, ledger_path=ledger_path,
        records=records, exchanges=exchanges, facts=facts, source=source))


def _runtime(context: CaseContext, *, runtime: Path, bundle: PreparedBundle, grant: LiveAuthorization,
             ledger_path: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]],
             facts: dict[str, Any], source: Path) -> dict[str, Any] | None:
    names = [path.relative_to(runtime).as_posix() for path in (runtime / "records").glob("*.json")]
    approvals = [path.relative_to(runtime).as_posix() for path in (runtime / "work/receiver").glob("approval-*.json")]
    if len(names) != 1 or len(approvals) != 1:
        return None
    fingerprints = {}
    def read(name: str) -> Any:
        body = (runtime / name).read_bytes()
        fingerprints[name] = {"bytes": len(body), "sha256": sha256(body).hexdigest()}
        return body if name == "materials/payload.txt" else json.loads(body)
    values = {name: read(name) for name in (*FILES, names[0], approvals[0])}
    if records != [values[names[0]]] or exchanges != [values["work/receiver/exchange.json"]]:
        raise SharedControlError("Current formal-save observed records or exchanges differ from their originals.")
    version = {**bundle.environment, "source_files": bundle.files}
    prepared_inputs, grant_inputs = legacy_runtime_inputs(bundle, grant)
    snapshot = FormalSaveObservation(context.case_id, context.protocol, context.mode, context.run_id, facts, version,
        environment_facts(), current_sources(source), runtime, values, names[0], approvals[0], prepared_inputs,
        json.loads(ledger_path.read_bytes()), grant_inputs)
    matched = match(snapshot, json.loads(REFERENCE.read_bytes()))
    if matched is not None:
        matched["original_evidence"] = fingerprints
    return matched
