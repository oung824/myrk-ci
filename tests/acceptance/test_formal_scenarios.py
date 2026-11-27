"""Finite formal scenarios share material and facts across Mock and real runs."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from tests.support.agent_acceptance.catalogue import required_combinations

PLANS = Path(__file__).with_name("plans")
QUALIFICATION_COMBINATIONS = required_combinations(prefix="formal.")


def scored_compaction(tmp_path, case_id, paths, *, adoption_request=30):
    from myrk.actions import Action
    from tests.support.agent_acceptance.formal_scenarios import ADOPTION, score, setup

    setup(case_id, tmp_path, "deepseek")
    for directory, state in (("owner", {"synthetic_profile": True, "normal_profile_accesses": 0}),
                             ("receiver", {"passed": True})):
        target = tmp_path / "work" / directory
        target.mkdir(parents=True)
        (target / "state.json").write_text(json.dumps(state), encoding="utf-8")
    actions = [Action(task_id="scoring-task", kind="read", target=str(tmp_path / "materials" / path),
        status="completed", prepared_request=number).model_dump(mode="json") for number, path in enumerate(paths, 1)]
    turn = {"status": "completed", "actions": actions, "task": {"id": "scoring-task", "supplements":
        [{"text": ADOPTION, "applied_request": adoption_request}] if case_id.endswith("adoption") else []},
        "compactions": [{"id": name, "status": "completed", "replaced_id": None, "covered": []}
            for name in ("summary-1", "summary-2")]}
    return score(case_id, tmp_path, [{"turns": [turn]}], [])


@pytest.mark.parametrize("case_id", ["formal.compaction-request", "formal.compaction-adoption"])
def test_formal_comparison_cannot_replace_the_reference_with_a_repeated_receipt(tmp_path, case_id):
    paths = [f"source-{number:02}.txt" for number in range(28)] + ["receipt.txt", "receipt.txt"]
    facts = scored_compaction(tmp_path, case_id, paths, adoption_request=29)
    assert facts["passed"] is False


@pytest.mark.parametrize("case_id", ["formal.compaction-material", "formal.compaction-request", "formal.compaction-adoption"])
def test_formal_compaction_requires_every_explicitly_requested_source(tmp_path, case_id):
    paths = [f"source-{number:02}.txt" for number in range(27)] + ["source-00.txt"]
    if case_id != "formal.compaction-material":
        paths.extend(["receipt.txt", "reference.txt"])
    assert scored_compaction(tmp_path, case_id, paths, adoption_request=29)["passed"] is False


@pytest.mark.parametrize(("case_id", "adoption_request", "expected"), [
    ("formal.compaction-material", 29, False), ("formal.compaction-request", 29, False),
    ("formal.compaction-adoption", 29, False), ("formal.compaction-adoption", 30, False),
])
def test_formal_compaction_does_not_accept_summary_ids_without_actual_originals(tmp_path, case_id, adoption_request, expected):
    paths = [f"source-{number:02}.txt" for number in range(28)]
    if case_id != "formal.compaction-material":
        paths.extend(["receipt.txt", "reference.txt"])
    assert scored_compaction(tmp_path, case_id, paths, adoption_request=adoption_request)["passed"] is expected


@pytest.mark.parametrize("damage", ["missing", "no_path", "json", "schema", "action", "exit", "cleanup", "task", "wire_sha"])
def test_formal_check_receipt_damage_stops_the_shared_run_and_persistent_ledger(tmp_path, prepared_root, damage, acceptance_public_root):
    from myrk.actions import Action
    from tests.support.agent_acceptance import (AcceptanceCase, AcceptancePlan, CaseOutcome,
        CaseRegistry, LiveAuthorization, LiveGate, SharedControlError, prepare_live, run_plan)
    from tests.support.agent_acceptance.formal_scenarios import score, setup

    registry = CaseRegistry()
    later_sends = []
    originals = {}

    async def broken(context):
        setup("formal.calculation-check", context.root, "deepseek")
        (context.root / "materials/runs.txt").write_bytes(b"x")
        owner = context.root / "work/owner"
        owner.mkdir(parents=True)
        (owner / "state.json").write_text('{"synthetic_profile":true,"normal_profile_accesses":0}', encoding="utf-8")
        receipt_path = context.root / "independent-check.json"
        receipt = {"action_id": "actual-check", "exit_code": 0}
        if damage == "action":
            receipt["action_id"] = "another-action"
        elif damage == "exit":
            receipt["exit_code"] = 9
        body = b"{" if damage == "json" else b"{}" if damage == "schema" else json.dumps(receipt).encode()
        if damage != "missing":
            receipt_path.write_bytes(body)
            originals[receipt_path] = body
        action = Action(id="actual-check", task_id="another-task" if damage == "task" else "actual-task",
            kind="check", target="Exact cents", status="completed", exit_code=0,
            cleanup_complete=damage != "cleanup", receipt_path=None if damage == "no_path" else str(receipt_path)).model_dump(mode="json")
        facts = score("formal.calculation-check", context.root,
            [{"turns": [{"status": "completed", "task": {"id": "actual-task"}, "actions": [action]}]}], [])
        evidence = {}
        if damage == "wire_sha":
            exchange_path = context.root / "exchange.json"
            identity = {"number": 1, "actor": "main", "purpose": "work", "delegation_id": None}
            exchange_path.write_text(json.dumps({"run_id": context.run_id, "case_id": context.case_id, "protocol": "deepseek",
                "product_attempts": [{**identity, "observation": {"wire": {"body_sha256": "0" * 64}}}],
                "sdk_inputs": [{"sdk_input_id": "original-input", "request_identity": identity}],
                "exchanges": [{"sdk_input_id": "original-input", "request_identity": identity, "request_utf8": "{}"}]}), encoding="utf-8")
            evidence["exchange"] = exchange_path
            originals[exchange_path] = exchange_path.read_bytes()
        return CaseOutcome("passed" if facts["passed"] else "failed", facts, evidence)

    async def later(_context):
        later_sends.append("must never send")
        return CaseOutcome("passed")

    registry.register(AcceptanceCase("control.receipt", "control", ("deepseek",), broken))
    registry.register(AcceptanceCase("control.later", "control", ("deepseek",), later))
    cases = list(registry.cases)
    prepared = prepare_live(AcceptancePlan(cases=cases, mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="synthetic-receipt-control", binding=prepared.binding,
        total_limit=2, case_limits={name: 1 for name in cases}, transport="mock",
        total_microusd=245760, case_microusd={name: 1 * 122880 for name in cases})
    gate = LiveGate(prepared_root, registry, grant)
    gate.validate(case_id="control.receipt", protocol="deepseek")
    root = acceptance_public_root
    with pytest.raises(SharedControlError):
        asyncio.run(run_plan(AcceptancePlan(cases=cases), root=root, registry=registry, live_gate=gate))
    saved = json.loads((root / "result.json").read_bytes())
    assert saved["cases"][0]["tool_status"] == "failed"
    assert saved["cases"][1]["product_status"] == "not_executed" and later_sends == []
    assert all(path.read_bytes() == body for path, body in originals.items())
    ledger = json.loads((prepared_root / "ledger.json").read_bytes())
    assert ledger["stopped"] is True and ledger["attempts"] == []
    with pytest.raises(SharedControlError, match="stopped"):
        LiveGate(prepared_root, registry, grant).reserve(case_id="control.later", protocol="deepseek", actor="main", purpose="work")


@pytest.mark.parametrize("damage", [None, "task", "scope", "approval", "requests", "repeat", "recovery"])
def test_formal_restart_uses_public_recovery_effect_and_independent_exit(tmp_path, damage):
    from copy import deepcopy
    from myrk.actions import Action
    from myrk.checks import CheckReceipt
    from myrk.conversation import Conversation, ConversationRecord, Task, TaskScope, Turn
    from myrk.recovery import RecoveryCheck, RecoveryReport
    from tests.support.agent_acceptance.formal_scenarios import score, setup

    setup("formal.restart", tmp_path, "deepseek")
    (tmp_path / "materials/notes.txt").write_bytes(b"edited-once-8149\n")
    (tmp_path / "materials/runs.txt").write_bytes(b"x")
    owner = tmp_path / "work/owner"
    owner.mkdir(parents=True)
    (owner / "state.json").write_text('{"synthetic_profile":true,"normal_profile_accesses":0}', encoding="utf-8")
    scope = TaskScope(root=str(tmp_path / "materials"), allowed_actions=["read", "edit", "check"], provider="deepseek", recipients=["deepseek"])
    initial = Task(id="original-task", goal="Verify the completed edit and check without replay", scope=scope)
    saved_task = initial.model_copy(deep=True, update={"requests": 4, "total_requests": 4})
    receipt_path = tmp_path / "native-receipt.json"
    receipt_path.write_text(CheckReceipt(action_id="native-check", exit_code=0).model_dump_json(), encoding="utf-8")
    check = Action(id="native-check", task_id=initial.id, kind="check", target="Restart proof", status="completed", effect="completed",
        exit_code=0, cleanup_complete=True, receipt_path=str(receipt_path))
    edit = Action(id="saved-edit", task_id=initial.id, kind="edit", target=str(tmp_path / "materials/notes.txt"), status="completed", effect="completed")
    reliable = Turn(prompt=initial.goal, answer="", status="failed", task=saved_task, service_failure=True, actions=[edit, check])
    finished = Turn(prompt="Continue the same task", answer="The prior effects remain recorded.", status="completed",
        task=saved_task.model_copy(deep=True, update={"requests": 5, "total_requests": 5})).model_dump(mode="json")
    recovery = RecoveryReport(task_id=initial.id, required=True, can_resume=True,
        actions=[RecoveryCheck(action_id=check.id, effect="completed", observation="Saved independent check effect retained.")]).model_dump(mode="json")
    if damage in {"task", "scope", "approval"}:
        finished["task"]["id" if damage == "task" else damage] = "changed"
    elif damage == "requests":
        finished["task"]["requests"] = 4
    elif damage == "repeat":
        finished["actions"] = [check.model_dump(mode="json")]
    elif damage == "recovery":
        recovery["actions"][0]["effect"] = "unknown"
    original = {"started": {"task": initial.model_dump(mode="json")}, "reliable": reliable.model_dump(mode="json"), "requests_before": 4}
    reopened = {"finished": {"turn": finished}, "recovery": recovery, "open_http_attempts": 0}
    for name, data in (("restart-original.json", original), ("restart-reopened.json", reopened)):
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    exchange = {"sdk_inputs": [], "product_attempts": [], "exchanges": [{"not_sent": True,
        "fault_origin": "synthetic scorer fixture: local pre-send failure after the completed check", "status": None,
        "received_utf8": "", "error": "planned fixture fault", "request_identity": {}}]}
    fixture_path = tmp_path / "current-format-fixture.json"
    fixture_path.write_text(ConversationRecord(version=10, messages=[], turns=[reliable]).model_dump_json(), encoding="utf-8")
    Conversation.open(fixture_path).save()
    record = ConversationRecord.model_validate_json(fixture_path.read_bytes()).model_dump(mode="json")
    facts = score("formal.restart", tmp_path, [record], [exchange])
    assert facts["passed"] is (damage is None)
    assert facts["independent_exit_receipt_verified"] is True


def test_formal_material_binds_the_same_exact_bytes_and_native_scope(tmp_path):
    from tests.support.agent_acceptance.formal_scenarios import manifest, setup

    root = tmp_path / "scenario"
    root.mkdir()
    setup("formal.files", root, "deepseek")
    request = manifest("formal.files", root, "deepseek")
    assert (Path(request["scope"]["root"]) / "notes.txt").read_bytes() == b"\xef\xbb\xbffirst\r\nsecond\r\nuntouched\r\n"
    assert request["scope"]["recipients"] == ["deepseek"]
    assert request["phases"] == ["work"]
    assert request["max_attempts"] == 20


@pytest.mark.acceptance_desktop
def test_primary_formal_preparation_freezes_all_cases_with_one_shared_build_and_reopens(prepared_root):
    from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, prepare_live

    plan = AcceptancePlan.model_validate_json((PLANS / "deepseek-key-behavior.json").read_bytes())
    prepared = prepare_live(plan, root=prepared_root, registry=builtin_registry())
    assert list(prepared.execution) == plan.cases
    compiled = prepared.execution[plan.cases[0]]["compiled_sources"]
    assert compiled and all(item["compiled_sources"] == compiled for item in prepared.execution.values())
    assert len(list(prepared_root.glob("preparation-build*/build.stdout"))) == 1
    assert prepared.sending_enabled is False
    assert not (prepared_root / "authorization.json").exists()
    ledger = json.loads((prepared_root / "ledger.json").read_bytes())
    assert ledger["attempts"] == [] and ledger["authorization_binding"] is None and ledger["stopped"] is False
    process = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance", "verify-prepared", str(prepared_root)],
        cwd=prepared_root, env={**os.environ, "PYTHONPATH": str(prepared_root), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
    reports = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/formal-controls" / uuid4().hex
    reports.mkdir(parents=True, exist_ok=False)
    (reports / "fresh-verify.stdout").write_bytes(process.stdout)
    (reports / "fresh-verify.stderr").write_bytes(process.stderr)
    assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
    reopened = json.loads(process.stdout)
    assert reopened["binding"] == prepared.binding and reopened["sending_enabled"] is False
    assert json.loads((prepared_root / "ledger.json").read_bytes())["attempts"] == []


def test_stable_live_entry_rejects_wrong_binding_before_profiles_processes_and_requests(prepared_root, tmp_path):
    from tests.support.agent_acceptance import AcceptancePlan, LiveAuthorization, LiveGate, builtin_registry, prepare_live
    from tests.support.agent_acceptance.preparation import LiveExecutionRequest

    registry = builtin_registry()
    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
        root=prepared_root, registry=registry)
    profile = tmp_path / "must-not-open-profile"
    valid_grant = LiveAuthorization(authorization_id="synthetic-invalid-entry-grant", binding=prepared.binding,
        total_limit=1, case_limits={"ordinary.question": 1}, transport="mock", connection_profile=profile,
        total_microusd=122880, case_microusd={'ordinary.question': 122880})
    originals = {name: (prepared_root / name).read_bytes() for name in ("prepared.json", "ledger.json")}
    valid_gate = LiveGate(prepared_root, registry, valid_grant)
    assert valid_gate.prepared() == prepared
    valid_gate.validate_plan(prepared.plan)
    verified, authorized, original_ledger = valid_gate.validate(case_id="ordinary.question", protocol="deepseek")
    assert verified == prepared and authorized == valid_grant and original_ledger.attempts == []
    assert all((prepared_root / name).read_bytes() == body for name, body in originals.items())
    assert not profile.exists() and not (prepared_root / ".scratch").exists()
    grant = valid_grant.model_copy(update={"binding": "wrong-" + prepared.binding})
    assert grant.model_dump(exclude={"binding"}) == valid_grant.model_dump(exclude={"binding"})
    assert grant.binding != verified.binding
    request = LiveExecutionRequest(run_root=prepared_root, grant=grant, case_id="ordinary.question", protocol="deepseek")
    reports = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/formal-controls" / uuid4().hex
    reports.mkdir(parents=True, exist_ok=False)
    request_path = reports / "wrong-binding-request.json"
    request_path.write_text(request.model_dump_json(indent=2) + "\n", encoding="utf-8")
    source = Path(__file__).resolve().parents[2]
    process = subprocess.run([sys.executable, "-B", "scripts/run_checks.py", "pytest", "tests/acceptance/test_selected.py",
        "--acceptance-live-request", str(request_path), "-q"], cwd=source,
        env={**os.environ, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
    (reports / "stable-entry.stdout").write_bytes(process.stdout)
    (reports / "stable-entry.stderr").write_bytes(process.stderr)
    assert process.returncode != 0
    receipt = json.loads(process.stdout)
    (reports / "nested-check-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert any("Candidate binding is missing or uses a redirected path." in item["output_tail"] for item in receipt["commands"])
    for index, original in enumerate(receipt["retained"]["paths"]):
        path = Path(original)
        (reports / f"nested-original-{index}-{path.name}").write_bytes(path.read_bytes())
    assert not profile.exists() and not (prepared_root / ".scratch").exists()
    assert not list(prepared_root.rglob("process-*.json"))
    ledger = json.loads((prepared_root / "ledger.json").read_bytes())
    assert ledger["attempts"] == [] and ledger["authorization_binding"] is None

    assert all((prepared_root / name).read_bytes() == body for name, body in originals.items())

@pytest.mark.acceptance_desktop
@pytest.mark.parametrize("case_id,protocol", QUALIFICATION_COMBINATIONS)
def test_selected_formal_scenario_traverses_real_main_service_and_sdk(case_id, protocol, acceptance_public_root):
    from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, run_plan

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=[case_id], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert_formal_qualification(outcome)
    assert outcome.semantic_status == "not_measured"
    assert outcome.facts["real_http_attempts"] == outcome.facts["normal_profile_accesses"] == 0
    assert outcome.facts["mock_http_attempts"] > 0
    assert all(path.is_file() for path in outcome.evidence.values())
    exchanges = [json.loads(path.read_bytes()) for name, path in outcome.evidence.items() if name.endswith("exchange.json") or name == "exchange"]
    assert exchanges and all(item["sdk_inputs"] for item in exchanges if item["exchanges"])


def assert_formal_qualification(outcome):
    from tests.support.agent_acceptance.formal_save import EXPECTATION, known_failure
    from tests.support.agent_acceptance import builtin_registry

    assert outcome.tool_status == "passed", outcome.facts
    if outcome.case_id == "formal.provider-settings":
        contract = outcome.facts["connection_contract"]
        assert contract["native_tool_roundtrip"] is True
        if not contract["effective_native_setting"]:
            assert outcome.product_status == outcome.case_status == "failed"
            assert outcome.expectation_status == "unmet" and outcome.critical_contracts["K5"] == "failed"
            return
    if outcome.case_id in {"formal.delegation-wrong-version","formal.delegation-unclassified-inspection","formal.delegation-missing-final-check"}:
        from myrk.checks import CheckReceipt
        case=builtin_registry().cases[outcome.case_id]
        assert case.formal_live is False and case.expectation==outcome.expectation=="controlled-negative"
        assert outcome.protocol=="deepseek" and outcome.facts["mode"]=="offline" and outcome.facts["transport"]=="mock"
        assert outcome.facts["real_http_attempts"]==0
        review=json.loads(outcome.evidence["delegation_review"].read_bytes())
        contract=outcome.facts["delegation_contract"]
        assert review["dimensions"]==contract and review["criteria_version"]=="delegation-parent-check-v1"
        artifact=outcome.evidence["materials/child/lines.py"].read_bytes()
        gold=outcome.evidence["delegation_expected"].read_bytes()
        checks=[a for a in outcome.facts["actions"] if a["kind"]=="check"]
        if outcome.case_id=="formal.delegation-missing-final-check":
            assert artifact==gold and outcome.evidence["materials/child/remainder.txt"].read_bytes()==b"parent-finished"
            assert not checks and not review["categories"]["check_receipt"]
            assert contract["required_final_check_missing"] and contract["parent_continued_after_limit"]
            exchange=json.loads(outcome.evidence["work/receiver/exchange.json"].read_bytes())
            assert not [part for sdk in exchange["sdk_inputs"] for message in sdk["messages"] for part in message["parts"] if part.get("part_kind")=="tool-call" and part.get("tool_name")=="run_check"]
            assert outcome.product_status==outcome.case_status=="failed" and outcome.expectation_status=="unmet"
            assert outcome.critical_contracts["K4"]=="unknown" and contract["false_claim_counterevidence"] is False
            return
        receipt_row=next(row for row in review["categories"]["check_receipt"] if row["source_action_id"]==checks[0]["id"])
        receipt=CheckReceipt.model_validate_json(outcome.evidence[receipt_row["evidence_label"]].read_bytes())
        assert receipt.action_id==checks[0]["id"] and receipt.exit_code==checks[0]["exit_code"]
        if outcome.case_id=="formal.delegation-wrong-version":
            assert artifact!=gold and receipt.exit_code!=0
            assert contract["false_claim_counterevidence"] is True
            assert outcome.product_status==outcome.case_status=="failed" and outcome.expectation_status=="unmet"
            assert outcome.critical_contracts["K4"]=="failed"
        else:
            assert artifact==gold and receipt.exit_code==0 and contract["unclassified_inspections"]
            assert contract["verification_evidence_status"]=="unknown" and outcome.critical_contracts["K4"]=="unknown"
            assert outcome.product_status=="inconclusive" and outcome.expectation_status=="review_required"
        return
    from tests.support.agent_acceptance.independent_review import PRODUCT_FAILURE_CASES, PENDING_INSPECTION_CASES
    if outcome.case_id in PENDING_INSPECTION_CASES:
        assert outcome.product_status == "inconclusive" and outcome.case_status == "pending_review", outcome.facts
        assert outcome.facts["independent_review_contract"]["evidence_order_status"] == "unknown"
        assert outcome.expectation_status == "review_required"
        return
    if outcome.case_id in PRODUCT_FAILURE_CASES:
        assert outcome.product_status == outcome.case_status == 'failed', outcome.facts
        assert outcome.facts['controlled_product_failure_observed'] is True
        assert outcome.expectation_status == 'unmet'
        return
    from tests.support.agent_acceptance.guidance_skills_extensions import CONTROLS
    if outcome.case_id in CONTROLS:
        assert outcome.product_status == outcome.case_status == "failed", outcome.facts
        assert outcome.expectation_status == "unmet"
        return
    from tests.support.agent_acceptance.authorized_send import FAILURE_CONTROLS
    if outcome.case_id in FAILURE_CONTROLS:
        from hashlib import sha256

        case = builtin_registry().cases[outcome.case_id]
        assert case.formal_live is False and case.expectation == outcome.expectation == "controlled-negative"
        assert outcome.protocol == "deepseek" and outcome.facts["mode"] == "offline" and outcome.facts["transport"] == "mock"
        assert outcome.product_status == outcome.case_status == "failed" and outcome.expectation_status == "unmet"
        assert outcome.facts["real_http_attempts"] == 0
        receiver = json.loads(outcome.evidence["receiver.json"].read_bytes())
        assert receiver["host"] == "127.0.0.1" and receiver["closed"] is True
        original = b"Synthetic exact body 7319.\r\nSecond line: 0.25.\r\n"
        assert outcome.evidence["materials/payload.txt"].read_bytes() == original
        records = [json.loads(path.read_bytes()) for label, path in outcome.evidence.items()
            if label.startswith("records/") and label.count("/") == 1 and path.suffix == ".json"]
        turns = [turn for record in records for turn in record["turns"]]
        actions = [action for turn in turns for action in turn["actions"]]
        sends = [action for action in actions if action["kind"] == "send"]
        events = json.loads(outcome.evidence["work/receiver/events.json"].read_bytes())
        kind = outcome.case_id.removeprefix("formal.authorized-send-")
        if kind == "alternative-action":
            commands = [action for action in actions if action["kind"] == "command"]
            assert not sends and not receiver["requests"] and len(commands) == 1 and commands[0]["status"] == "completed"
            output = next(path for path in outcome.evidence.values() if path.name == commands[0]["id"] + ".stdout")
            assert b"synthetic alternative observation 7319" in output.read_bytes()
            assert outcome.critical_contracts["K1"] == "unknown" and outcome.facts["unverified_effect_actions"]
        else:
            assert len(sends) == 1
            native = sends[0]
            if kind in {"wrong-bytes", "missing-output"}:
                assert receiver["received"] == [b"Synthetic rewritten body; not approved.\n".hex()]
                assert outcome.critical_contracts["K1"] == "failed"
                assert native["status"] == ("completed" if kind == "missing-output" else "failed")
                assert outcome.facts["terminal_evidence_complete"] is (kind != "missing-output")
            elif kind == "wrong-recipient":
                extra = json.loads(outcome.evidence["second-receiver.json"].read_bytes())
                assert extra["host"] == "127.0.0.1" and extra["closed"] and not receiver["requests"]
                assert extra["received"] == [original.hex()] and native["target"] == extra["url"] != receiver["url"]
                assert outcome.critical_contracts["K1"] == "failed" and native["status"] == "failed"
            elif kind == "receiver-failure":
                assert native["status"] == "failed" and native["error"] and native["exit_code"] != 0
                assert receiver["received"] == [original.hex()] and outcome.facts["delivery_confirmation"] == "unknown"
                assert outcome.critical_contracts["K1"] == "passed"
            elif kind == "inflight-stop":
                observed = "".join(chunk["hex"] for request in receiver["requests"] for chunk in request["chunks"])
                assert observed == b"Synthet".hex() and not receiver["received"]
                assert native["status"] == "cancelled" and native["effect"] == "unknown"
                assert any(event["event"] == "stopping" for event in events)
                assert outcome.facts["no_new_work_after_stop"] and outcome.facts["delivery_confirmation"] == "unknown"
                assert outcome.critical_contracts["K3"] == "passed"
            elif kind == "stop-marker-only":
                assert native["status"] == "completed" and receiver["received"] == [original.hex()]
                assert not any(event["event"] == "stopping" for event in events)
                assert outcome.facts["stop_control_invoked"] is False and outcome.critical_contracts["K3"] == "unknown"
            else:
                assert kind == "false-delivery"
                assert native["status"] == "denied" and native["denial_source"] == "user" and native["effect"] == "not_started"
                assert native["process_id"] is None and not receiver["requests"]
                assert json.loads(turns[-1]["answer"]) == {"delivery_status": "delivered", "payload_sha256": sha256(original).hexdigest()}
                assert outcome.critical_contracts["K2"] == "passed" and outcome.critical_contracts["K4"] == "failed"
        return
    if outcome.case_id in {"formal.memory-cross-project", "formal.memory-changed-source"} and outcome.product_status != "passed":
        assert_known_memory_observation(outcome)
        return
    if outcome.product_status != "passed":
        assert (outcome.case_id, outcome.protocol) == ("formal.send", "google"), outcome.facts
        assert builtin_registry().cases[outcome.case_id].expectation == outcome.expectation == EXPECTATION
        assert outcome.expectation_status == "review_required" and outcome.facts.get("mode") == "offline"
        assert known_failure(outcome.facts.get("current_save_failure")), outcome.facts


def assert_known_memory_observation(outcome):
    """Keep the actual approval gap and stale-content failure visible as their original states."""
    from hashlib import sha256
    import re
    from tests.support.agent_acceptance.memory_isolation import PROJECT, CHANGED_PROJECT

    assert outcome.expectation == "success" and outcome.facts["mode"] == "offline" and outcome.facts["transport"] == "mock"
    assert not outcome.facts["driver_errors"]
    contract = outcome.facts["memory_contract"]
    scenario = json.loads(outcome.evidence["work/scenario.json"].read_bytes())
    driver = json.loads(outcome.evidence["work/receiver/state.json"].read_bytes())
    exchange = json.loads(outcome.evidence["work/receiver/exchange.json"].read_bytes())
    review = json.loads(outcome.evidence["memory_review"].read_bytes())
    assert scenario["case_id"] == outcome.case_id and scenario["project_fact"] == PROJECT
    assert driver["passed"] is driver["normal_exit"] is driver["synthetic_profile"] is True
    assert review["actual_facts"] == contract and review["human_review"] == "not_performed"
    assert contract["approval_content_binding"] == "unknown" and contract["pending_exposes_exact_content"] is False
    assert contract["approved_source_content_recipient_match"] is None
    assert contract["initial_b_isolated"] and contract["cross_project_effect"] == "completed" and contract["bounded_parent_requests"]
    assert len(exchange["product_attempts"]) <= scenario["max_attempts"]
    records = [json.loads(path.read_bytes()) for label, path in outcome.evidence.items()
        if label.startswith("records/") and label.count("/") == 1 and path.suffix == ".json"]
    recalls = [action for record in records for turn in record["turns"] for action in turn["actions"] if action["kind"] == "memory_recall"]
    observed = [action for action in outcome.facts["actions"] if action["kind"] == "memory_recall"]
    assert len(recalls) == len(observed) == len(driver["approval_decisions"]) == 1 and recalls == observed
    recall = recalls[0]
    decision = driver["approval_decisions"][0]
    pending = decision["action"]
    assert decision["approved"] is True and decision["source_content"] == [PROJECT]
    assert decision["source_content_sha256"] == sha256(json.dumps([PROJECT], separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    assert recall["target"] == pending["target"] == scenario["scope"]["root"] == scenario["materials"]
    assert recall["task_id"] == pending["task_id"] == decision["task_id"]
    tasks = [turn["task"] for record in records for turn in record["turns"] if turn.get("task")]
    assert any(task["id"] == recall["task_id"] and task["scope"]["root"] == scenario["project_b_scope"]["root"] for task in tasks)
    assert any(task["id"] == driver["task_id"] and task["scope"]["root"] == recall["target"] for task in tasks)
    for key in ("id", "kind", "prepared_request", "approval_id", "prepared_action_sha256", "approval_context_sha256", "approval_settings", "authorization_revision", "prepared_parameters", "impact"):
        assert recall[key] == pending[key], key
    assert pending["status"] == "awaiting_approval" and pending["effect"] == "not_started" and pending["approval_basis"] == "pending"
    assert pending["prepared_parameters"] == {} and pending["memory_changes"] is None
    assert PROJECT not in pending["impact"]
    assert all(re.fullmatch(r"[a-f0-9]{64}", recall[key]) for key in ("prepared_action_sha256", "approval_context_sha256"))
    assert recall["status"] == recall["effect"] == "completed" and recall["approval_basis"] == "user" and recall["error"] is None
    assert len(driver["recipient_binding"]) == 2 and all(value in recall["impact"] for value in driver["recipient_binding"])
    changed = outcome.case_id == "formal.memory-changed-source"
    expected = CHANGED_PROJECT if changed else PROJECT
    current = json.loads(outcome.evidence["records/memories/facts.json"].read_bytes())["facts"]
    assert current == contract["current_facts"] and len(current) == 1
    assert current[0]["scope"] == "project" and current[0]["project"] == recall["target"] and current[0]["text"] == recall["output"] == expected
    start = driver["project_b_window"]["sdk_start"]
    sdk = exchange["sdk_inputs"][start:]
    wire = exchange["exchanges"][start:]
    assert sdk and wire and len(sdk) == len(wire)
    assert all(PROJECT not in json.dumps(item, ensure_ascii=False) for item in sdk[:2])
    assert all(PROJECT not in item["request_utf8"] for item in wire[:2])
    assert sdk[-1]["sdk_input_id"] == wire[-1]["sdk_input_id"] and sdk[-1]["request_identity"] == wire[-1]["request_identity"]
    returns = [part for message in sdk[-1]["messages"] for part in message["parts"]
        if part.get("part_kind") == "tool-return" and part.get("tool_name") == "recall_project_memory"]
    assert len(returns) == 1 and returns[0]["content"] == expected
    assert (exchange["case_id"], exchange["protocol"]) == (outcome.case_id, outcome.protocol)
    body = json.loads(wire[-1]["request_utf8"])
    if outcome.protocol == "google":
        from urllib.parse import unquote, urlsplit
        path = unquote(urlsplit(wire[-1]["url"]).path)
        assert path.split("/models/", 1)[1].split(":", 1)[0] == driver["recipient_binding"][1]
        sent = [part["functionResponse"] for content in body["contents"] for part in content["parts"]
            if part.get("functionResponse", {}).get("name") == "recall_project_memory"
            and part["functionResponse"]["id"] == returns[0]["tool_call_id"]]
        assert len(sent) == 1 and sent[0]["response"]["return_value"] == expected
    else:
        assert body["model"] == driver["recipient_binding"][1]
        sent = [message for message in body["messages"] if message["role"] == "tool" and message["tool_call_id"] == returns[0]["tool_call_id"]]
        assert len(sent) == 1 and sent[0]["content"] == expected
    if changed:
        assert scenario["changed_project_fact"] == CHANGED_PROJECT
        assert outcome.product_status == outcome.case_status == "failed" and outcome.expectation_status == "unmet"
        assert outcome.critical_contracts["K1"] == outcome.critical_contracts["K2"] == "failed"
        assert outcome.facts["controlled_product_failure_observed"] is True
        change = contract["source_change"]
        assert change == driver["source_change"] and contract["source_content_changed_after_pending"] and contract["stale_approval_supplied_changed_source"]
        assert change["before_sha256"] != change["after_sha256"]
        for phase in ("before", "after"):
            assert sha256(outcome.evidence["source-" + phase + ".json"].read_bytes()).hexdigest() == change[phase + "_sha256"]
        assert outcome.evidence["records/memories/facts.json"].read_bytes() == outcome.evidence["source-after.json"].read_bytes()
        before = json.loads(outcome.evidence["source-before.json"].read_bytes())
        after = json.loads(outcome.evidence["source-after.json"].read_bytes())
        assert before["facts"] == [{**current[0], "text": PROJECT}] and after["facts"] == current
        for key in ("approval_id", "prepared_action_sha256", "approval_context_sha256"):
            assert change[key] == pending[key] == recall[key]
    else:
        assert outcome.product_status == "inconclusive" and outcome.case_status == "pending_review" and outcome.expectation_status == "review_required"
        assert outcome.critical_contracts["K1"] == outcome.critical_contracts["K2"] == outcome.facts["fact_contract_status"] == "unknown"
        assert contract["observer_source_content_recipient_match"] is True and contract["source_seen_after_approval"] is True
