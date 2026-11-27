"""The actual saved failure is recognized without repeating its execution."""

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

import pytest

MATERIAL = Path(__file__).parent / "materials/formal_save"
SOURCE = Path(__file__).resolve().parents[2]


@pytest.fixture
def candidate(monkeypatch):
    """Exercise the historical classifier with its frozen source and environment observations."""
    from tests.support.agent_acceptance import formal_save
    reference = json.loads((MATERIAL / "reference.json").read_bytes())
    monkeypatch.setattr(formal_save, "current_sources", lambda _source: dict(reference["source_pins"]))
    monkeypatch.setattr(formal_save, "environment_facts", lambda: {
        "python": reference["python"], "packages": dict(reference["packages"])})
    return formal_save


@pytest.fixture
def saved(tmp_path):
    from zipfile import ZipFile
    index = json.loads((MATERIAL / "current-google-send-index.json").read_bytes())
    archive = MATERIAL / index["archive"]
    assert sha256(archive.read_bytes()).hexdigest() == index["archive_sha256"]
    with ZipFile(archive) as originals:
        for entry in index["files"]:
            body = originals.read(entry["path"])
            assert len(body) == entry["bytes"] and sha256(body).hexdigest() == entry["sha256"]
            path = tmp_path / "saved" / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
    return tmp_path / "saved"


def test_saved_original_is_a_product_failure_with_complete_tool_observation(saved, candidate):
    before = {path: path.read_bytes() for path in saved.rglob("*") if path.is_file()}
    known = candidate.recognize(saved, source=SOURCE)
    assert known is not None
    assert known["rule"] == "current-formal-send-google-save-failure-v1"
    assert known["product_status"] == "failed" and known["tool_status"] == "passed"
    assert known["expectation_status"] == "review_required"
    assert known["historical_relation"] == known["root_cause"] == "unknown"
    assert known["missing_underlying_exception"] is True
    assert before == {path: path.read_bytes() for path in saved.rglob("*") if path.is_file()}


@pytest.mark.parametrize("observation", ["source", "python", "package"])
def test_saved_failure_does_not_match_a_different_current_observation(saved, candidate, monkeypatch, observation):
    assert candidate.recognize(saved, source=SOURCE) is not None
    if observation == "source":
        sources = candidate.current_sources(SOURCE)
        sources["myrk/conversation.py"] = "0" * 64
        monkeypatch.setattr(candidate, "current_sources", lambda _source: sources)
    else:
        environment = candidate.environment_facts()
        if observation == "python": environment["python"] = "different-python"
        else: environment["packages"]["pydantic-ai-slim"] = "different-package"
        monkeypatch.setattr(candidate, "environment_facts", lambda: environment)
    assert candidate.recognize(saved, source=SOURCE) is None


def replace_original(saved, name, value, *, bind=True):
    directory = saved / "formal.send/google"
    path = directory / name
    body = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(body)
    if bind:
        index_path = directory / "evidence-index.json"
        index = json.loads(index_path.read_bytes())
        index["links"][name].update(sha256=sha256(body).hexdigest(), bytes=len(body))
        index_path.write_text(json.dumps(index), encoding="utf-8")


@pytest.mark.parametrize("damage", ["case", "protocol", "live", "python", "sdk_version", "product_source", "native_driver",
    "notice", "error", "role", "ownership", "payload", "send_effect", "received", "open_receiver", "basis", "approval",
    "chronology", "driver_extra", "driver_frame", "process", "compiled", "read_failed", "request_count", "prepared_payload", "extra_error",
    "call_id", "action_owner", "approval_record", "process_owner"])
def test_similar_incomplete_or_differently_bound_failures_remain_unclassified(saved, candidate, damage):
    result_path = saved / "result.json"
    result = json.loads(result_path.read_bytes())
    case = result["cases"][0]
    directory = saved / "formal.send/google"
    names = {"record": next((directory / "records").glob("*.json")).relative_to(directory).as_posix(),
        "exchange": "work/receiver/exchange.json", "receiver": "receiver.json", "events": "work/receiver/events.json",
        "state": "work/receiver/state.json", "process": "work/processes.json", "approval": next((directory / "work/receiver").glob("approval-*.json")).relative_to(directory).as_posix()}
    values = {key: json.loads((directory / name).read_bytes()) for key, name in names.items()}
    turn = values["record"]["turns"][0]
    if damage == "case": case["case_id"] = "formal.files"
    elif damage == "protocol": case["protocol"] = "deepseek"
    elif damage == "live": result["mode"] = "live"
    elif damage == "python": result["version"]["python"] += "changed"
    elif damage == "sdk_version": result["version"]["packages"]["google-genai"] = "unknown"
    elif damage == "product_source": result["version"]["source_files"]["myrk/conversation.py"] = "0" * 64
    elif damage == "native_driver": result["version"]["source_files"]["tests/support/agent_acceptance/desktop/scenarios.mjs"] = "0" * 64
    elif damage == "notice": values["record"]["save_failures"][1] = values["record"]["save_failures"][1].replace("19:40:53.238576", "19:40:53.239576")
    elif damage == "error": turn["error"] += " different"
    elif damage == "role": values["exchange"]["sdk_inputs"][1]["request_identity"]["actor"] = "action_review"
    elif damage == "ownership": values["exchange"]["task_id"] = "another-task"
    elif damage == "payload":
        for message in values["exchange"]["sdk_inputs"][1]["messages"]:
            message["parts"] = [part for part in message["parts"] if part["part_kind"] != "tool-return"]
    elif damage == "send_effect": turn["actions"][1]["effect"] = "completed"
    elif damage == "received": values["receiver"]["received"] = ["unexpected payload"]
    elif damage == "open_receiver": values["receiver"]["closed"] = False
    elif damage == "basis": turn["actions"][1]["approval_basis"] = "user_approved"
    elif damage == "approval": values["approval"]["approved"] = False
    elif damage == "chronology": values["events"][30], values["events"][32] = values["events"][32], values["events"][30]
    elif damage == "driver_extra": case["facts"]["driver_errors"] *= 2
    elif damage == "driver_frame": values["state"]["error"] = values["state"]["error"].replace(":105:31", ":106:31")
    elif damage == "process": values["process"]["processes"][0]["exit_code"] = 9
    elif damage == "compiled": case["facts"]["compiled_sources_match"] = False
    elif damage == "read_failed": turn["actions"][0]["status"] = "failed"
    elif damage == "request_count": turn["task"]["requests"] = 3
    elif damage == "prepared_payload": turn["actions"][1]["prepared_parameters"]["payload_sha256"] = "0" * 64
    elif damage == "extra_error": values["events"].insert(31, {"event": "error", "message": "unknown failure"})
    elif damage == "call_id":
        for message in values["exchange"]["sdk_inputs"][1]["messages"]:
            for part in message["parts"]:
                if part["part_kind"] == "tool-return": part["tool_call_id"] = "another-call"
    elif damage == "action_owner": turn["actions"][1]["task_id"] = "another-task"
    elif damage == "approval_record": values["approval"]["record"] = "another-record.json"
    elif damage == "process_owner": values["process"]["processes"][0]["direct_spawn"]["identity"]["created_filetime"] = "0"
    for key, name in names.items(): replace_original(saved, name, values[key])
    result_path.write_text(json.dumps(result), encoding="utf-8")
    assert candidate.recognize(saved, source=SOURCE) is None


@pytest.mark.parametrize("damage", ["ledger", "ledger_stopped", "bytes", "index_path", "control_error", "missing", "json"])
def test_corrupt_originals_and_ledger_retain_the_shared_control_stop(saved, candidate, damage):
    from tests.support.agent_acceptance.core import SharedControlError
    directory = saved / "formal.send/google"
    if damage.startswith("ledger"):
        ledger = json.loads((directory / "ledger.json").read_bytes())
        if damage == "ledger": ledger["attempts"][1]["number"] = 9
        else: ledger["stopped"] = True
        replace_original(saved, "ledger.json", ledger)
    elif damage == "bytes":
        path = directory / "work/receiver/exchange.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "index_path":
        path = directory / "evidence-index.json"
        index = json.loads(path.read_bytes()); index["links"]["receiver.json"]["path"] = "../another-file"
        path.write_text(json.dumps(index), encoding="utf-8")
    elif damage == "control_error":
        transport = json.loads((directory / "work/receiver/transport.json").read_bytes())
        transport["control_error"] = "control rejected"
        replace_original(saved, "work/receiver/transport.json", transport)
    elif damage == "missing": (directory / "work/receiver/events.json").unlink()
    else: (directory / "work/receiver/events.json").write_bytes(b"{")
    with pytest.raises(SharedControlError): candidate.recognize(saved, source=SOURCE)


def test_tool_classifier_source_changes_do_not_pin_its_preceding_implementation(saved, candidate):
    directory = saved / "formal.send/google"
    path = saved / "result.json"
    result = json.loads(path.read_bytes())
    prepared = json.loads((directory / "prepared.json").read_bytes())
    for name in ("formal_scenarios.py", "formal.py", "reporting.py", "catalogue.py"):
        key = "tests/support/agent_acceptance/" + name
        result["version"]["source_files"][key] = prepared["files"][key] = "1" * 64
    fingerprint = {key: prepared[key] for key in ("root", "source_root", "plan", "files", "execution", "environment")}
    prepared["binding"] = candidate.digest(fingerprint)
    ledger = json.loads((directory / "ledger.json").read_bytes())
    ledger["binding"] = prepared["binding"]
    ledger["authorization_binding"] = candidate.digest({"authorization_id": "offline-qualification-" + result["run_id"],
        "binding": prepared["binding"], "total_limit": 10, "case_limits": {"formal.send": 10}, "transport": "mock", "connection_profile": None})
    replace_original(saved, "prepared.json", prepared)
    replace_original(saved, "ledger.json", ledger)
    path.write_text(json.dumps(result), encoding="utf-8")
    assert candidate.recognize(saved, source=SOURCE) is not None


def test_retained_derivation_keeps_the_failed_original_and_missing_cause(saved, candidate):
    reports = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/formal-save-offline-replay"
    shutil.copytree(saved, reports)
    original = (reports / "result.json").read_bytes()
    known = candidate.recognize(reports, source=SOURCE)
    assert known is not None
    derived = json.loads(original)
    case = derived["cases"][0]
    case.update(product_status="failed", tool_status="passed", expectation_status="review_required")
    case["facts"].update(current_known_save_failure=known)
    derived["qualification"] = {"kind": "pure_saved_original_derivation", "actual_execution_repeated": False,
        "native_run": "b-nkjstxm_", "original_result_sha256": sha256(original).hexdigest(),
        "original_tool_status": "failed", "product_fix_claimed": False, "historical_relation": "unknown"}
    (reports / "derived-result.json").write_text(json.dumps(derived, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert (reports / "result.json").read_bytes() == original
    assert case["facts"]["driver_errors"] and known["missing_underlying_exception"] is True
    assert case["facts"]["driver_completed"] is False


@pytest.mark.parametrize("field", ["task_id", "kind", "target", "prepared_request", "prepared_parameters", "impact", "event_action", "terminal_error"])
def test_approval_cannot_refer_to_a_different_immutable_candidate_after_rebinding(saved, candidate, field):
    directory = saved / "formal.send/google"
    approval_name = next((directory / "work/receiver").glob("approval-*.json")).relative_to(directory).as_posix()
    approval = json.loads((directory / approval_name).read_bytes())
    state = json.loads((directory / "work/receiver/state.json").read_bytes())
    if field == "task_id": approval["action"][field] = "another-task"
    elif field == "kind": approval["action"][field] = "write"
    elif field == "target": approval["action"][field] = "http://127.0.0.1:55922/another-recipient"
    elif field == "prepared_request": approval["action"][field] = 1
    elif field == "prepared_parameters": approval["action"][field]["payload_sha256"] = "0" * 64
    elif field == "impact": approval["action"][field] = "A different payload and recipient."
    elif field == "terminal_error":
        record_path = next((directory / "records").glob("*.json"))
        record = json.loads(record_path.read_bytes())
        record["turns"][0]["actions"][1]["error"] = "Another product failure."
        replace_original(saved, record_path.relative_to(directory).as_posix(), record)
        result_path = saved / "result.json"
        result = json.loads(result_path.read_bytes())
        result["cases"][0]["facts"]["actions"] = record["turns"][0]["actions"]
        result_path.write_text(json.dumps(result), encoding="utf-8")
        events = json.loads((directory / "work/receiver/events.json").read_bytes())
        events[33]["turn"]["actions"][1]["error"] = "Another product failure."
        replace_original(saved, "work/receiver/events.json", events)
    else:
        approval["event"]["action"]["target"] = "http://127.0.0.1:55922/another-recipient"
        events = json.loads((directory / "work/receiver/events.json").read_bytes())
        events[29] = approval["event"]
        replace_original(saved, "work/receiver/events.json", events)
    state["approval_decisions"] = [approval]
    replace_original(saved, approval_name, approval)
    replace_original(saved, "work/receiver/state.json", state)
    assert candidate.recognize(saved, source=SOURCE) is None


@pytest.mark.parametrize("desktop_timeout", [False, True])
def test_formal_boundary_preserves_known_product_failure_and_checks_desktop_exit(saved, candidate, desktop_timeout):
    from tests.support.agent_acceptance.formal import scenario_outcome

    case = json.loads((saved / "result.json").read_bytes())["cases"][0]
    known = candidate.recognize(saved, source=SOURCE)
    assert known is not None
    assert case["tool_status"] == case["product_status"] == "failed"
    if desktop_timeout:
        case["facts"]["desktop_exits"] = {"work": {
            "root_exit": {"code": None, "signal": "SIGTERM"},
            "failure": "Formal desktop qualification timed out",
        }}
    outcome = scenario_outcome(case["facts"], {}, current_failure=known)
    assert outcome.product_status == "failed"
    assert outcome.tool_status == ("failed" if desktop_timeout else "passed")
    assert outcome.facts["current_save_failure"] == known
    assert outcome.facts["driver_errors"] == case["facts"]["driver_errors"]


@pytest.mark.parametrize("damage", [None, "other_protocol", "real", "records", "exchanges", "unknown_error", "parent", "plan_field", "explicit_plan_default", "grant_field"])
def test_runtime_adapter_uses_real_saved_inputs_without_a_published_result_or_index(saved, candidate, damage):
    from tests.support.agent_acceptance.core import CaseContext, SharedControlError
    from tests.support.agent_acceptance.preparation import LiveAuthorization, PreparedBundle
    directory = saved / "formal.send/google"
    result = json.loads((saved / "result.json").read_bytes())
    case = result["cases"][0]
    bundle = PreparedBundle.model_validate_json((directory / "prepared.json").read_bytes())
    grant = LiveAuthorization(authorization_id="offline-qualification-" + result["run_id"], binding=bundle.binding,
        total_limit=10, case_limits={"formal.send": 10}, transport="mock")
    records = [json.loads(next((directory / "records").glob("*.json")).read_bytes())]
    exchanges = [json.loads((directory / "work/receiver/exchange.json").read_bytes())]
    context = CaseContext(directory, "google", result["run_id"], "formal.send")
    if damage == "other_protocol": context = CaseContext(directory, "deepseek", result["run_id"], "formal.send")
    elif damage == "real": grant = grant.model_copy(update={"transport": "real"})
    elif damage == "parent": grant = grant.model_copy(update={"parent_budget_id": "another-parent"})
    elif damage == "grant_field": grant = grant.model_copy(update={"total_limit": 9})
    elif damage in {"plan_field", "explicit_plan_default"}:
        original = json.loads((directory / "prepared.json").read_bytes())
        if damage == "plan_field": original["plan"]["cases"] = ["ordinary.question"]
        else: original["plan"]["historical_groups"] = []
        bundle = PreparedBundle.model_validate(original)
    elif damage == "records": records[0]["turns"][0]["status"] = "completed"
    elif damage == "exchanges": exchanges[0]["task_id"] = "other-task"
    elif damage == "unknown_error": case["facts"]["driver_errors"].append({"path": "unknown", "error": "unknown"})
    (saved / "result.json").unlink()
    (directory / "evidence-index.json").unlink()
    invoke = lambda: candidate.recognize_runtime(context, runtime=directory, bundle=bundle, grant=grant,
        ledger_path=directory / "ledger.json", records=records, exchanges=exchanges, facts=case["facts"], source=SOURCE)
    if damage in {"records", "exchanges", "plan_field", "explicit_plan_default"}:
        with pytest.raises(SharedControlError): invoke()
    else:
        matched = invoke()
        assert (matched is not None) is (damage is None)
        if matched is not None:
            assert candidate.known_failure(matched) and matched["original_evidence"]
    assert not (saved / "result.json").exists() and not (directory / "evidence-index.json").exists()


@pytest.mark.parametrize("damage", [None, "protocol", "case", "mode", "rule", "expectation", "tool", "unmatched"])
def test_public_formal_assertion_accepts_only_static_google_and_precisely_matched_failure(saved, candidate, damage):
    from tests.acceptance.test_formal_scenarios import assert_formal_qualification
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.formal import scenario_outcome
    from tests.support.agent_acceptance.reporting import assess
    case = json.loads((saved / "result.json").read_bytes())["cases"][0]
    known = candidate.recognize(saved, source=SOURCE)
    outcome = scenario_outcome(case["facts"], {}, current_failure=known)
    outcome.case_id, outcome.protocol = "formal.send", "google"
    assess(builtin_registry().cases[outcome.case_id], outcome)
    if damage == "protocol": outcome.protocol = "deepseek"
    elif damage == "case": outcome.case_id = "formal.files"
    elif damage == "mode": outcome.facts["mode"] = "live"
    elif damage == "rule": outcome.facts["current_save_failure"]["rule"] = "unknown"
    elif damage == "expectation": outcome.expectation = "success"
    elif damage == "tool": outcome.tool_status = "failed"
    elif damage == "unmatched": outcome.facts.pop("current_save_failure")
    if damage is None:
        assert outcome.expectation_status == "review_required"
        assert_formal_qualification(outcome)
    else:
        with pytest.raises(AssertionError): assert_formal_qualification(outcome)


def test_static_expectation_cannot_accept_failed_other_protocol_or_arbitrary_marker(saved, candidate):
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.formal import scenario_outcome
    from tests.support.agent_acceptance.reporting import assess
    case = json.loads((saved / "result.json").read_bytes())["cases"][0]
    known = candidate.recognize(saved, source=SOURCE)
    for protocol, matched in (("google", None), ("deepseek", known), ("openai-chat", known)):
        outcome = scenario_outcome(case["facts"], {}, current_failure=matched)
        outcome.case_id, outcome.protocol = "formal.send", protocol
        assess(builtin_registry().cases[outcome.case_id], outcome)
        assert outcome.expectation_status == "unmet"


def test_new_material_helper_issue_and_checkout_attribute_enter_actual_source_binding():
    from tests.support.agent_acceptance import AcceptancePlan, builtin_registry
    from tests.support.agent_acceptance.preparation import bound_files
    files = bound_files(AcceptancePlan(cases=["formal.send"], protocols=["google"]), builtin_registry())
    assert {"tests/support/agent_acceptance/formal_save.py", "tests/acceptance/materials/formal_save/reference.json",
        "tests/acceptance/materials/formal_save/current-google-send.zip", "tests/acceptance/test_formal_save.py",
        "tests/support/agent_acceptance/desktop/.gitattributes", candidate_issue()} <= files.keys()


def candidate_issue():
    return ".scratch/acceptance-save-failure-diagnosis-20261113/issues/02-formal-google-send.md"


@pytest.mark.parametrize(("count", "corrupt"), [(3, False), (4, False), (3, True)])
def test_synthetic_complete_success_and_other_valid_request_counts_are_not_shared_failures(saved, candidate, count, corrupt):
    """A complete synthetic runtime contract is not evidence of a new native Google run."""
    from copy import deepcopy
    from tests.support.agent_acceptance.core import CaseContext, SharedControlError
    from tests.support.agent_acceptance.formal import scenario_outcome
    from tests.support.agent_acceptance.preparation import LiveAuthorization, PreparedBundle
    directory = saved / "formal.send/google"
    original = json.loads((saved / "result.json").read_bytes())
    facts = original["cases"][0]["facts"]
    bundle = PreparedBundle.model_validate_json((directory / "prepared.json").read_bytes())
    grant = LiveAuthorization(authorization_id="offline-qualification-" + original["run_id"], binding=bundle.binding,
        total_limit=10, case_limits={"formal.send": 10}, transport="mock")
    record_path = next((directory / "records").glob("*.json"))
    record = json.loads(record_path.read_bytes())
    exchange = json.loads((directory / "work/receiver/exchange.json").read_bytes())
    ledger = json.loads((directory / "ledger.json").read_bytes())
    turn = record["turns"][0]
    send = turn["actions"][1]
    send.update(status="completed", effect="completed", approval_basis="user_approved", error=None, output="Sent exact payload.")
    turn.update(status="completed", error=None, answer="The approved send completed.")
    turn["task"].update(requests=count, total_requests=count, turns_used=count)
    record["save_failures"] = []
    for number in range(3, count + 1):
        attempt = deepcopy(turn["request_attempts"][-1])
        attempt.update(number=number, own_number=number)
        sdk = deepcopy(exchange["sdk_inputs"][-1])
        sdk["sdk_input_id"] = "synthetic-normal-sdk-" + str(number)
        identity = {"number": number, "actor": "main", "purpose": "work", "delegation_id": None}
        sdk["request_identity"] = identity
        tool_return = deepcopy(next(part for message in sdk["messages"] for part in message["parts"] if part["part_kind"] == "tool-return"))
        tool_return.update(tool_name="send_text", tool_call_id=send["tool_call_id"], content="Sent exact payload.")
        sdk["messages"].extend([deepcopy(record["messages"][-1]), {"kind": "request", "parts": [tool_return]}])
        request = deepcopy(exchange["exchanges"][-1])
        body = json.loads(request["request_utf8"])
        body["contents"].extend([{"role": "model", "parts": [{"functionCall": {"name": "send_text", "args": send["prepared_parameters"]}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "send_text", "response": {"return_value": "Sent exact payload."}}}]}])
        request.update(attempt=number, sdk_input_id=sdk["sdk_input_id"], request_identity=identity,
            request_utf8=json.dumps(body), received_utf8='data: {"candidates":[{"content":{"role":"model","parts":[{"text":"The approved send completed."}]},"finishReason":"STOP"}]}\n\n')
        attempt["observation"]["wire"]["body_sha256"] = sha256(request["request_utf8"].encode()).hexdigest()
        turn["request_attempts"].append(attempt)
        exchange["product_attempts"].append({"task_id": turn["task"]["id"], **deepcopy(attempt)})
        exchange["sdk_inputs"].append(sdk)
        exchange["exchanges"].append(request)
        ledger["attempts"].append({**ledger["attempts"][-1], "number": number, "product_number": number})
    if corrupt: ledger["attempts"][-1]["number"] = 99
    record_path.write_text(json.dumps(record), encoding="utf-8")
    (directory / "work/receiver/exchange.json").write_text(json.dumps(exchange), encoding="utf-8")
    (directory / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    state_path = directory / "work/receiver/state.json"
    state = json.loads(state_path.read_bytes()); state.update(passed=True, error=None)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    events_path = directory / "work/receiver/events.json"
    events = json.loads(events_path.read_bytes())
    events = [event for event in events if event.get("event") != "error"
              and not (event.get("event") == "status" and "进展未能保存" in event.get("text", ""))]
    for event in events:
        if event.get("event") == "finished": event["turn"] = deepcopy(turn)
    events_path.write_text(json.dumps(events), encoding="utf-8")
    receiver_path = directory / "receiver.json"
    receiver = json.loads(receiver_path.read_bytes()); receiver["received"] = ["exact-approved-send-body"]
    receiver_path.write_text(json.dumps(receiver), encoding="utf-8")
    transport_path = directory / "work/receiver/transport.json"
    transport = json.loads(transport_path.read_bytes()); transport["mock_http_attempts"] = count
    transport_path.write_text(json.dumps(transport), encoding="utf-8")
    facts.update(passed=True, tool_contract_passed=True, driver_errors=[], driver_completed=True, actions=turn["actions"],
        unrelated_failure_action_ids=[], mock_http_attempts=count, action_statuses=[[action["kind"], action["status"]] for action in turn["actions"]])
    facts.pop("failure_classification", None)
    facts.pop("product_execution", None)
    (saved / "result.json").unlink()
    (directory / "evidence-index.json").unlink()
    context = CaseContext(directory, "google", original["run_id"], "formal.send")
    invoke = lambda: candidate.recognize_runtime(context, runtime=directory, bundle=bundle, grant=grant,
        ledger_path=directory / "ledger.json", records=[record], exchanges=[exchange], facts=facts, source=SOURCE)
    if corrupt:
        with pytest.raises(SharedControlError): invoke()
    else:
        assert invoke() is None
        outcome = scenario_outcome(facts, {}, expected_phases=1)
        assert outcome.tool_status == outcome.product_status == "passed"
        from tests.support.agent_acceptance import builtin_registry
        from tests.support.agent_acceptance.reporting import assess
        from tests.acceptance.test_formal_scenarios import assert_formal_qualification
        outcome.case_id, outcome.protocol = "formal.send", "google"
        assess(builtin_registry().cases[outcome.case_id], outcome)
        assert outcome.expectation_status == "met"
        assert_formal_qualification(outcome)
