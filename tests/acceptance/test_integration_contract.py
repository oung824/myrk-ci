"""The stable entry retains coverage, failures, and trustworthy evidence."""

import asyncio
from hashlib import sha256
import json
import os
import subprocess
import sys

import pytest

from tests.support.agent_acceptance import (AcceptanceCase, AcceptancePlan, CaseOutcome,
    CaseRegistry, SharedControlError, builtin_registry, run_plan)


def test_builtin_catalogue_exposes_all_seventeen_executable_groups():
    registry = builtin_registry()
    rows = registry.catalogue()
    assert {row["group"] for row in rows} == {
        "conversation", "context-tools", "actions", "review", "delegation", "memory",
        "queue-parallel", "recovery", "lifecycle", "providers", "failures",
        "request-layout", "compaction", "honest-reporting", "deliverables", "text-diff-image-preview", "document-preview"}
    assert {row["case_id"] for row in rows} >= {"actions.files", "review.evidence-recovery",
        "memory.personal", "recovery.desktop-restart", "compaction.repeated", "desktop.handoff"}
    assert all(row["protocols"] and row["bindings"] and row["observation_layer"] and row["expectation"] for row in rows)


def test_a_required_branch_or_native_protocol_cannot_disappear_from_the_catalogue():
    from dataclasses import replace
    from tests.support.agent_acceptance.catalogue import validate_builtin_catalogue

    registry = builtin_registry()
    original = registry.cases.pop("delegation.stop")
    with pytest.raises(ValueError, match="Required acceptance catalogue"):
        validate_builtin_catalogue(registry)
    registry.cases[original.case_id] = replace(original, protocols=("deepseek",))
    with pytest.raises(ValueError, match="Required acceptance protocols"):
        validate_builtin_catalogue(registry)


def test_missing_evidence_link_stops_the_shared_run(tmp_path):
    registry = CaseRegistry()

    async def broken(context):
        return CaseOutcome("passed", evidence={"receipt": context.root / "missing.json"})

    async def later(_context):
        pytest.fail("A broken evidence association must stop the run")

    registry.register(AcceptanceCase("control.broken", "control", ("deepseek",), broken))
    registry.register(AcceptanceCase("control.later", "control", ("deepseek",), later))
    with pytest.raises(SharedControlError, match="evidence"):
        asyncio.run(run_plan(AcceptancePlan(cases=list(registry.cases)), root=tmp_path / "run", registry=registry))
    result = json.loads((tmp_path / "run/result.json").read_text(encoding="utf-8"))
    assert result["cases"][1]["product_status"] == "not_executed"


@pytest.mark.parametrize("exchange,missing_link,stop_status,expected", [
    (None, None, "failed", "failed"),
    (None, None, "passed", "unknown"),
    ({"run_id": "another-run", "exchanges": []}, None, "failed", "unknown"),
    ({"run_id": "another-run", "exchanges": []}, "first", "failed", "unknown"),
    ({"run_id": "another-run", "exchanges": []}, "last", "failed", "unknown"),
])
def test_incomplete_publication_retains_only_a_proven_stop_failure(tmp_path, exchange, missing_link, stop_status, expected):
    registry = CaseRegistry()

    async def scored(context):
        original = context.root / "exchange.json"
        if exchange is not None:
            original.write_text(json.dumps(exchange), encoding="utf-8")
        evidence = {"exchange": original}
        missing = {"receipt": context.root / "missing-receipt.json"}
        if missing_link == "first":
            evidence = {**missing, **evidence}
        if missing_link == "last":
            evidence.update(missing)
        return CaseOutcome("failed" if stop_status == "failed" else "passed",
            {"request_stop_status": "unknown", "action_stop_status": stop_status},
            evidence, critical_contracts={"K3": stop_status})

    async def later(_context):
        pytest.fail("Incomplete evidence must still stop the batch")

    registry.register(AcceptanceCase("quality.qg3.formal.9", "control", ("deepseek",), scored))
    registry.register(AcceptanceCase("control.later", "control", ("deepseek",), later))
    root = tmp_path / "run"
    with pytest.raises(SharedControlError, match="evidence"):
        asyncio.run(run_plan(AcceptancePlan(cases=list(registry.cases)), root=root, registry=registry))

    case, remaining = json.loads((root / "result.json").read_bytes())["cases"]
    assert case["tool_status"] == case["case_status"] == "failed"
    assert case["critical_contracts"]["K3"] == expected
    assert remaining["case_status"] == "not_executed"


def _stop_request_original(stop, *, run_id, case_id, protocol):
    inputs, rows, attempts = [], [], []
    for number in (1, 2):
        identity = {"number": number, "actor": "main", "purpose": "task", "delegation_id": None}
        step_id, request_body = f"step-{number}", json.dumps({"fixture_request": number})
        model_input = {"sdk_input_id": f"input-{number}", "request_identity": identity}
        inputs.append(model_input)
        rows.append({**model_input, "attempt": number, "product_step_id": step_id,
                     "request_utf8": request_body, "terminal_http_started": True,
                     "ledger_number": number, "transport_provenance": "fixed_fixture", "received_utf8": ""})
        attempts.append({**identity, "step_id": step_id,
                         "observation": {"wire": {"body_sha256": sha256(request_body.encode()).hexdigest()}}})
    return {
        "run_id": run_id, "case_id": case_id, "protocol": protocol, "task_id": stop["task_id"],
        "exchanges": rows, "sdk_inputs": inputs, "product_attempts": attempts,
        "stop_boundaries": [{"event": stop, "terminal_attempts": [{"attempt": 1, "sdk_input_id": "input-1"}]}],
    }


@pytest.mark.parametrize(("damaged", "diagnostic"), [
    ("sdk_inputs", "SDK input"),
    ("product_attempts", "product attempt"),
    ("request_utf8", "request body"),
])
def test_publication_preserves_proven_late_request_when_earlier_original_is_incomplete(tmp_path, damaged, diagnostic):
    from tests.support.agent_acceptance.quality_governance import refusal_request_status

    async def scored(context):
        stop = {"event": "stopping", "task_id": "stopped-task"}
        envelope = _stop_request_original(stop, run_id=context.run_id, case_id=context.case_id, protocol=context.protocol)
        saved = {"request_attempts": list(envelope["product_attempts"])}
        if damaged == "request_utf8":
            envelope["exchanges"][0]["request_utf8"] = ""
        else:
            envelope[damaged].pop(0)
        request_status = refusal_request_status(context.root, [envelope], stop, saved, inprocess=True)
        assert request_status == "failed"
        original = context.root / "exchange.json"
        original.write_text(json.dumps(envelope), encoding="utf-8")
        return CaseOutcome("failed", {"request_stop_status": request_status, "action_stop_status": "unknown"},
            {"exchange": original}, critical_contracts={"K3": request_status})

    async def later(_context):
        pytest.fail("The incomplete original must stop the batch")

    registry = CaseRegistry()
    registry.register(AcceptanceCase("quality.qg3.formal.9", "control", ("deepseek",), scored))
    registry.register(AcceptanceCase("control.later", "control", ("deepseek",), later))
    root = tmp_path / "run"
    with pytest.raises(SharedControlError, match=diagnostic):
        asyncio.run(run_plan(AcceptancePlan(cases=list(registry.cases)), root=root, registry=registry))

    case, remaining = json.loads((root / "result.json").read_bytes())["cases"]
    assert case["tool_status"] == "failed"
    assert case["critical_contracts"]["K3"] == case["facts"]["request_stop_status"] == "failed"
    assert case["evidence_status"] == "incomplete"
    assert remaining["case_status"] == "not_executed"


@pytest.mark.parametrize(("exchange", "late_action", "control_error", "other_issue", "expected"), [
    (None, True, None, None, "failed"),
    (b"", True, None, None, "failed"),
    (b'{"exchanges":', True, None, None, "failed"),
    (b"null", True, None, None, "failed"),
    pytest.param({"exchanges": None}, True, None, None, "failed", id="malformed-exchange-list"),
    (b"{}", True, "shared send control stopped", None, "unknown"),
    pytest.param(None, True, "shared send control stopped", None, "unknown", id="missing-exchange-with-control-error"),
    pytest.param(None, True, None, "unconfirmed-process", "unknown", id="missing-exchange-with-unconfirmed-process"),
    pytest.param({}, False, None, "missing-owner", "failed", id="missing-owner-with-confirmed-late-request"),
    pytest.param({"run_id": "foreign-run"}, False, None, "missing-owner", "unknown", id="missing-owner-with-foreign-run"),
    pytest.param({"case_id": "foreign-case"}, False, None, "missing-owner", "unknown", id="missing-owner-with-foreign-case"),
    pytest.param({"protocol": "openai-chat"}, False, None, "missing-owner", "unknown", id="missing-owner-with-foreign-protocol"),
    pytest.param({}, False, None, "wrong-reservation", "unknown", id="missing-owner-with-wrong-reservation"),
    pytest.param({}, False, None, "missing-earlier-row", "failed", id="earlier-http-row-missing"),
    pytest.param({}, False, None, "earlier-sdk-ledger-owner", "unknown", id="missing-http-row-with-sdk-ledger-conflict"),
    pytest.param({}, False, None, "earlier-snapshot-ledger-state", "unknown", id="missing-http-row-with-stop-ledger-conflict"),
    pytest.param({}, False, None, "stop-snapshot-http-ordinal", "unknown", id="complete-originals-with-stop-http-conflict"),
    pytest.param({}, False, None, "stop-snapshot-ledger-ordinal", "unknown", id="missing-http-sdk-id-with-stop-ordinal-conflict"),
    pytest.param({}, False, None, "earlier-sdk-ledger-input-id", "unknown", id="missing-http-row-with-sdk-ledger-input-id-conflict"),
    pytest.param({}, False, None, "earlier-ledger-sdk-input-id", "unknown", id="missing-http-row-with-ledger-sdk-input-id-conflict"),
    pytest.param({}, False, None, "missing-earlier-sdk-id", "failed", id="earlier-sdk-id-missing-but-ledger-agrees"),
    pytest.param({}, False, None, "wrong-earlier-ledger-body", "unknown", id="earlier-sdk-id-missing-with-ledger-body-conflict"),
    pytest.param({}, False, None, "wrong-earlier-ledger-identity", "unknown", id="earlier-sdk-id-missing-with-ledger-identity-conflict"),
    pytest.param({}, False, None, "late-reservation-only", "unknown", id="late-http-start-with-reservation-only"),
    pytest.param({}, False, None, "wrong-late-ledger-step", "unknown", id="late-ledger-step-conflicts"),
    pytest.param({}, False, None, "wrong-late-product-step", "unknown", id="late-product-capture-step-conflicts"),
    pytest.param({}, False, None, "wrong-sdk-ledger-owner", "unknown", id="missing-http-identity-with-sdk-ledger-conflict"),
    pytest.param({}, False, None, "not-sent-at-stop", "unknown", id="missing-http-marker-with-stop-ledger-conflict"),
    pytest.param({}, False, None, "wrong-saved-wire", "unknown", id="early-saved-conversation-wire-conflicts"),
    pytest.param({}, False, None, "wrong-saved-step", "unknown", id="early-saved-conversation-step-conflicts"),
    pytest.param({}, False, None, "missing-saved-wire", "failed", id="early-saved-wire-missing-only"),
    pytest.param({}, True, None, "invalid-late-utf8", "failed", id="damaged-late-request-still-checks-actions"),
    pytest.param({}, False, None, "malformed-no-send", "failed", id="malformed-no-send-flag-still-checks-late-request"),
    pytest.param({}, False, None, "malformed-product-actor", "failed", id="malformed-early-product-actor-still-checks-late-request"),
    (None, False, None, None, "unknown"),
])
def test_formal_stop_verdict_survives_failed_original_validation(
    tmp_path, monkeypatch, exchange, late_action, control_error, other_issue, expected,
):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import Mock

    from tests.support.agent_acceptance import formal, formal_scenarios, quality_runtime
    from tests.support.agent_acceptance.core import IncompleteEvidenceError
    from tests.support.agent_acceptance.preparation import _LiveLedger, _ReservedAttempt

    case_id = "quality.qg3.formal.9"
    plan = AcceptancePlan(cases=[case_id, "control.later"], mode="live")
    accounting = tmp_path / "accounting"
    (accounting / "desktop/node_modules").mkdir(parents=True)
    ledger = _LiveLedger(version=2, integrity="fixture", binding="synthetic-stop",
                        authorization_binding=None, stopped=False, attempts=[])
    (accounting / "prepared.json").write_bytes(b"{}")
    (accounting / "ledger.json").write_text(ledger.model_dump_json(), encoding="utf-8")
    bundle = SimpleNamespace(root=accounting, plan=plan, execution={case_id: {"compiled_sources": {}}},
                             candidate_directory=None, files={})
    gate = Mock(root=accounting)
    gate.prepared.return_value = bundle
    gate.validate.return_value = bundle, Mock(transport="mock", **{"model_dump.return_value": {}}), None
    gate.owner.return_value = nullcontext()
    gate.running.return_value = nullcontext()
    gate._read_ledger.return_value = SimpleNamespace(candidates=[])
    gate.accounting_report.return_value = {"stopped": False, "case_stops": {}}
    gate.execution_id.side_effect = lambda name, protocol: f"test-execution:{name}:{protocol}"

    visible = quality_runtime.QualityVisibleInput(task_id="QG3.formal.9", goal="Stop after the refusal.",
        adapter="qg3-9", sources={}, parameters={"candidate": "prohibited.txt"})
    task = SimpleNamespace(group="QG3", task_id=visible.task_id, material_family_id="stop-evidence", domain_standards={})
    monkeypatch.setattr(quality_runtime, "bound_task", lambda *_: (task, visible, None))
    monkeypatch.setattr(quality_runtime, "product_permission_status", lambda *_: {"status": "unknown"})
    monkeypatch.setattr(formal_scenarios, "setup", lambda *_: None)
    monkeypatch.setattr(formal_scenarios, "bind_execution", lambda *_: None)
    monkeypatch.setattr(formal_scenarios, "manifest", lambda *_: {"phases": ["work"]})
    monkeypatch.setattr(quality_runtime, "finish", lambda *_: None)

    def build(directory):
        directory.mkdir()
        for name in ("build.stdout", "build.stderr"):
            (directory / name).write_bytes(b"")
        return {}

    original_exchange = None

    async def replay_runner(*_args, **kwargs):
        nonlocal original_exchange
        from pathlib import Path

        authority = json.loads(Path(kwargs["env"]["MYRK_ACCEPTANCE_AUTHORITY"]).read_bytes())
        phase = Path(authority["runtime"])
        record = phase.parent / "records/conversation.json"
        stop = {"event": "stopping", "task_id": "stopped-task", "path": str(record)}
        request_attempts = []
        if isinstance(exchange, dict):
            envelope = _stop_request_original(stop, run_id=authority["run_id"], case_id=case_id, protocol="deepseek")
            request_attempts = [attempt.copy() for attempt in envelope["product_attempts"]]
            ledger.attempts[:] = [
                _ReservedAttempt(
                    number=row["ledger_number"], authorization_id="synthetic-stop", case_id=case_id, protocol="deepseek",
                    actor="main", purpose="task", product_number=row["attempt"], product_step_id=row["product_step_id"],
                    execution_id=gate.execution_id(case_id, "deepseek"), reservation_microusd=0, occupied_microusd=0,
                    state="http_started", sdk_input_id=row["sdk_input_id"],
                    request_sha256=sha256(row["request_utf8"].encode()).hexdigest(),
                ) for row in envelope["exchanges"]
            ]
            if other_issue == "wrong-reservation":
                ledger.attempts[-1].sdk_input_id = "wrong-input-id"
            elif other_issue == "late-reservation-only":
                ledger.attempts[-1].state = "reserved"
            elif other_issue == "wrong-late-ledger-step":
                ledger.attempts[-1].product_step_id = "another-step"
            elif other_issue == "wrong-late-product-step":
                envelope["product_attempts"][-1]["step_id"] = "another-step"
            elif other_issue == "wrong-sdk-ledger-owner":
                del envelope["exchanges"][0]["request_identity"]
                ledger.attempts[0].actor = "delegation"
            elif other_issue == "not-sent-at-stop":
                del envelope["exchanges"][0]["terminal_http_started"]
                ledger.attempts[0].state = "not_sent"
            elif other_issue == "wrong-saved-wire":
                request_attempts[0]["observation"] = {"wire": {"body_sha256": sha256(b"wrong saved body").hexdigest()}}
            elif other_issue == "wrong-saved-step":
                request_attempts[0]["step_id"] = "wrong-saved-step"
            elif other_issue == "missing-saved-wire":
                del request_attempts[0]["observation"]
            elif other_issue == "invalid-late-utf8":
                envelope["exchanges"][-1]["request_utf8"] = chr(0xd800)
            elif other_issue == "malformed-no-send":
                envelope["exchanges"][0]["not_sent"] = "damaged"
            elif other_issue == "malformed-product-actor":
                envelope["product_attempts"][0]["actor"] = []
            elif other_issue == "missing-earlier-row":
                envelope["exchanges"].pop(0)
            elif other_issue == "earlier-sdk-ledger-owner":
                envelope["exchanges"].pop(0)
                envelope["product_attempts"].pop(0)
                envelope["sdk_inputs"][0]["request_identity"] = {
                    **envelope["sdk_inputs"][0]["request_identity"], "actor": "delegation",
                }
            elif other_issue == "earlier-snapshot-ledger-state":
                envelope["exchanges"].pop(0)
                ledger.attempts[0].state = "not_sent"
            elif other_issue == "stop-snapshot-http-ordinal":
                envelope["exchanges"][0]["attempt"] = 3
            elif other_issue == "stop-snapshot-ledger-ordinal":
                envelope["exchanges"][0]["attempt"] = 3
                del envelope["exchanges"][0]["sdk_input_id"]
            elif other_issue == "earlier-sdk-ledger-input-id":
                envelope["exchanges"].pop(0)
                envelope["sdk_inputs"][0]["sdk_input_id"] = "different-input"
            elif other_issue == "earlier-ledger-sdk-input-id":
                envelope["exchanges"].pop(0)
                ledger.attempts[0].sdk_input_id = "different-input"
            elif other_issue in {"missing-earlier-sdk-id", "wrong-earlier-ledger-body", "wrong-earlier-ledger-identity"}:
                del envelope["exchanges"][0]["sdk_input_id"]
                if other_issue == "wrong-earlier-ledger-body":
                    ledger.attempts[0].request_sha256 = sha256(b"different body").hexdigest()
                elif other_issue == "wrong-earlier-ledger-identity":
                    ledger.attempts[0].actor = "delegation"
            (accounting / "ledger.json").write_text(ledger.model_dump_json(), encoding="utf-8")
            original_exchange = json.dumps({**envelope, **exchange}).encode()
        else:
            original_exchange = exchange
        events = [stop]
        if late_action:
            events.append({**stop, "event": "action", "action": {
                "id": "late-action", "task_id": "stopped-task", "status": "started", "effect": "unknown",
            }})
        originals = {
            record: {"turns": [{"task": {"id": "stopped-task"}, "status": "cancelled", "actions": [],
                                 "project_trusted": False, "request_attempts": request_attempts}]},
            phase / "processes.json": {"complete": other_issue != "unconfirmed-process"},
            phase / "receiver/state.json": {"stop_accepted": True, "task_id": "stopped-task", "runtime_versions": {}},
            phase / "receiver/events.json": events,
            phase / "receiver/transport.json": {"control_error": control_error, "real_http_attempts": 0,
                "mock_http_attempts": len(request_attempts), "planned_local_transport_failures": 0},
            phase / "owner/state.json": {},
            phase / "relay/state.json": {},
        }
        if other_issue not in {None, "unconfirmed-process", "stop-snapshot-http-ordinal"}:
            del originals[phase / "owner/state.json"]
        for path, body in originals.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body), encoding="utf-8")
        if original_exchange is not None:
            (phase / "receiver/exchange.json").write_bytes(original_exchange)

        async def communicate():
            return b"", b""

        return SimpleNamespace(returncode=0, communicate=communicate)

    def validate(argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"", args=argv)

    monkeypatch.setattr(formal, "build", build)
    monkeypatch.setattr(formal, "subprocess", SimpleNamespace(run=validate))
    monkeypatch.setattr(formal, "asyncio", SimpleNamespace(to_thread=asyncio.to_thread,
        create_subprocess_exec=replay_runner, subprocess=asyncio.subprocess))

    async def unexpected(_context):
        pytest.fail("Formal dispatch must use the handoff and keep later cases stopped")

    registry = CaseRegistry()
    for name in plan.cases:
        registry.register(AcceptanceCase(name, "control", ("deepseek",), unexpected, formal_live=True))

    root = tmp_path / "run"
    with pytest.raises(SharedControlError) as error:
        result = asyncio.run(run_plan(plan, root=root, registry=registry, live_gate=gate))
        pytest.fail(f"Expected the batch to stop. Recorded outcomes: {[item.facts.get('error') for item in result.cases]}")

    case, remaining = json.loads((root / "result.json").read_bytes())["cases"]
    assert case["tool_status"] == "failed"
    assert case["critical_contracts"]["K3"] == expected
    if other_issue in {"wrong-earlier-ledger-body", "wrong-earlier-ledger-identity", "late-reservation-only",
                      "wrong-late-ledger-step", "wrong-late-product-step", "wrong-sdk-ledger-owner",
                      "not-sent-at-stop", "wrong-saved-wire", "wrong-saved-step", "earlier-sdk-ledger-owner",
                      "earlier-snapshot-ledger-state", "stop-snapshot-http-ordinal", "stop-snapshot-ledger-ordinal",
                      "earlier-sdk-ledger-input-id", "earlier-ledger-sdk-input-id"}:
        assert not isinstance(error.value, IncompleteEvidenceError)
    if other_issue == "missing-owner" and expected == "unknown":
        assert "association changed" in case["facts"]["error"]
    if other_issue == "wrong-reservation":
        assert "Gated request identity" in case["facts"]["error"]
    if other_issue in {"stop-snapshot-http-ordinal", "stop-snapshot-ledger-ordinal"}:
        assert "HTTP evidence conflicts with the stop snapshot" in case["facts"]["error"]
    if other_issue in {"earlier-sdk-ledger-input-id", "earlier-ledger-sdk-input-id"}:
        assert "Gated SDK input ID differs" in case["facts"]["error"]
    if other_issue == "unconfirmed-process":
        assert "process exit is unconfirmed" in case["facts"]["error"]
    if control_error:
        assert control_error in case["facts"]["error"]
    if expected == "failed":
        assert case["facts"]["request_stop_status"] == ("unknown" if late_action else "failed")
        assert case["facts"]["action_stop_status"] == ("failed" if late_action else "unknown")
        assert case["evidence_status"] == "incomplete"
    assert remaining["case_status"] == "not_executed"
    gate.stop.assert_called()
    if original_exchange is not None:
        assert (root / case_id / "deepseek/work/receiver/exchange.json").read_bytes() == original_exchange


def test_runtime_negative_claim_cannot_change_the_static_expectation(tmp_path):
    registry = CaseRegistry()

    async def spoofed(_context):
        return CaseOutcome("failed", {"planned_negative": True, "expectation_met": True})

    registry.register(AcceptanceCase("control.spoofed", "control", ("deepseek",), spoofed))
    result = asyncio.run(run_plan(AcceptancePlan(cases=list(registry.cases)), root=tmp_path / "run", registry=registry))
    assert result.cases[0].expectation_status == "unmet"


def test_run_records_actual_source_and_environment_fingerprints(tmp_path):
    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=tmp_path / "run", registry=builtin_registry()))
    saved = json.loads(result.result_path.read_text(encoding="utf-8"))
    assert saved["version"]["source_files"]["tests/support/agent_acceptance/core.py"]
    assert saved["version"]["packages"]["pydantic-ai-slim"]
    assert saved["measurements"]["current_real"] == "not_measured"
    assert result.cases[0].evidence["semantic_review"].is_file()


def test_fresh_execute_cannot_compete_with_the_existing_send_owner(prepared_root, tmp_path):
    from tests.support.agent_acceptance import LiveAuthorization, LiveGate, prepare_live

    registry = builtin_registry()
    prepared = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="synthetic-owner-test", binding=prepared.binding, total_limit=1,
        case_limits={"ordinary.question": 1}, transport="mock",
        total_microusd=122880, case_microusd={'ordinary.question': 122880})
    authority = tmp_path / "request.json"
    authority.write_text(json.dumps({"run_root": str(prepared_root), "grant": grant.model_dump(mode="json"),
        "case_id": "ordinary.question", "protocol": "deepseek"}), encoding="utf-8")
    with LiveGate(prepared_root, registry, grant).owner():
        process = subprocess.run([sys.executable, "-B", "-m", "tests.support.agent_acceptance.formal_cli", "execute",
            str(authority), "--output", str(tmp_path / "competing")], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
    assert process.returncode != 0 and b"Another formal execution owns" in process.stderr
    assert not (tmp_path / "competing").exists()
    assert json.loads((prepared_root / "ledger.json").read_bytes())["attempts"] == []


def test_default_executor_uses_the_shared_install_and_explicit_invalid_path_fails_before_execution(tmp_path):
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    environment.pop("MYRK_EXECUTOR_PATH", None)
    argv = [sys.executable, "-B", "-c", "from tests.support.agent_acceptance.execution import configure_executor; print(configure_executor())"]
    good = subprocess.run(argv, env=environment, capture_output=True)
    assert good.returncode == 0 and b"myrk\\runtime\\0.166.0" in good.stdout, good.stderr
    environment["MYRK_EXECUTOR_PATH"] = str(tmp_path / "missing.exe")
    bad = subprocess.run(argv, env=environment, capture_output=True)
    assert bad.returncode != 0 and b"installed pinned MYRK_EXECUTOR_PATH" in bad.stderr


def test_static_negative_and_known_defect_verdicts_preserve_product_failures():
    from pathlib import Path
    from uuid import uuid4

    cases = ["actions.record-write-failure", "actions.check-missing-receipt", "delegation.answer-divergence",
        "memory.save-failure", "compaction.early-no-history", "compaction.window-reopen"]
    root = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports/static-verdicts" / uuid4().hex
    result = asyncio.run(run_plan(AcceptancePlan(cases=cases), root=root, registry=builtin_registry()))
    assert [item.product_status for item in result.cases] == ["failed", "failed", "failed", "passed", "passed", "passed"]
    assert [item.expectation_status for item in result.cases] == ["met"] * 6
    assert all(item.tool_status == "passed" and item.semantic_status == "not_measured" for item in result.cases)
