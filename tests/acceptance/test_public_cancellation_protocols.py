"""Public user cancellation retains its Task and occupied cost across native protocols."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path

import pytest

from myrk.conversation import Conversation
from tests.acceptance.test_merger_t02_cancel_boundary import stopped_case
from tests.support.agent_acceptance import (
    AcceptanceCase, AcceptancePlan, CaseRegistry, LiveAuthorization, LiveGate,
    prepare_live, run_plan,
)
from tests.support.agent_acceptance.core import Protocol


@pytest.mark.parametrize("protocol", ["openai-chat", "google"])
def test_public_user_stop_preserves_task_and_unknown_budget_across_reopen(
    prepared_root: Path, acceptance_public_root: Path, protocol: Protocol,
):
    name = "budget.user-stop"
    registry = CaseRegistry()
    registry.register(AcceptanceCase(name, "request-input", (protocol,), stopped_case))
    bundle = prepare_live(
        AcceptancePlan(cases=[name], protocols=[protocol], mode="prepare-live"),
        root=prepared_root, registry=registry,
    )
    price = next(item for item in bundle.prices if item.protocol == protocol and item.role == "product")
    reservation = price.reservation_microusd
    grant = LiveAuthorization(
        authorization_id=f"synthetic-public-user-stop-{protocol}", binding=bundle.binding,
        total_limit=2, case_limits={name: 2}, total_microusd=2 * reservation,
        case_microusd={name: 2 * reservation}, transport="mock",
    )
    plan = AcceptancePlan(cases=[name], protocols=[protocol])
    gate = LiveGate(prepared_root, registry, grant)
    result = asyncio.run(run_plan(plan, root=acceptance_public_root, registry=registry, live_gate=gate))
    case = result.cases[0]
    assert case.product_status == "passed" and case.facts["turn_status"] == "cancelled"
    original = case.evidence["conversation"].read_bytes()
    saved = Conversation.open(case.evidence["conversation"]).turns[-1]
    exchange = json.loads(case.evidence["exchange"].read_bytes())
    assert saved.status == "cancelled" and "A partial answer has arrived." in saved.answer
    assert saved.task is not None and saved.task.id == exchange["task_id"]
    assert exchange["protocol"] == saved.task.provider_config.protocol == protocol
    assert saved.task.requests == saved.task.total_requests == saved.requests == 1
    assert len(saved.request_attempts) == len(exchange["product_attempts"]) == 1
    request = saved.request_attempts[0]
    assert exchange["product_attempts"][0] == request.model_dump(mode="json")
    assert len(exchange["sdk_inputs"]) == len(exchange["exchanges"]) == 1
    sdk, wire = exchange["sdk_inputs"][0], exchange["exchanges"][0]
    identity = {"number": request.number, "actor": "main", "purpose": "work", "delegation_id": None}
    assert sdk["request_identity"] == wire["request_identity"] == identity
    assert "A partial answer has arrived." in wire["received_utf8"]

    before = gate.accounting_report()
    assert before["reserved_requests"] == before["actual_http"] == 1
    assert len(before["attempts"]) == 1
    attempt = before["attempts"][0]
    assert attempt["execution_id"] == case.execution_id
    assert attempt["product_number"] == request.number and attempt["product_step_id"] == request.step_id
    assert (attempt["actor"], attempt["purpose"], attempt["delegation_id"]) == ("main", "work", None)
    assert attempt["sdk_input_id"] == sdk["sdk_input_id"] == wire["sdk_input_id"]
    assert request.observation is not None and request.observation.wire is not None
    assert attempt["request_sha256"] == request.observation.wire.body_sha256 == sha256(wire["request_utf8"].encode()).hexdigest()
    stop_key = f"{name}:{protocol}"
    assert set(before["case_stops"]) == {stop_key}
    assert before["case_stops"][stop_key]["source_sha256"] == sha256(original).hexdigest()
    assert attempt["state"] == "unknown"
    assert before["occupied_microusd"] == attempt["occupied_microusd"] == reservation
    assert attempt["usage_estimate_microusd"] is attempt["supplier_final_microusd"] is None

    reopened = LiveGate(prepared_root, registry, grant)
    again = asyncio.run(run_plan(plan, root=acceptance_public_root / "reopen", registry=registry, live_gate=reopened))
    after = reopened.accounting_report()
    assert again.cases[0].execution_id == case.execution_id
    assert after["reserved_requests"] == before["reserved_requests"]
    assert after["actual_http"] == before["actual_http"]
    assert after["attempts"] == before["attempts"] and after["case_stops"] == before["case_stops"]
    assert after["occupied_microusd"] == reservation
    reopened_turn = Conversation.open(case.evidence["conversation"]).turns[-1]
    assert reopened_turn.task == saved.task and reopened_turn.status == "cancelled"
    assert case.evidence["conversation"].read_bytes() == original
    print(json.dumps({"protocol": protocol, "execution_id": case.execution_id, "task_id": saved.task.id,
        "prepared_root": str(prepared_root), "saved_requests": saved.task.requests,
        "http_before": before["actual_http"], "http_after": after["actual_http"],
        "reserved_before": before["reserved_requests"], "reserved_after": after["reserved_requests"],
        "state": after["attempts"][0]["state"], "occupied_microusd": after["occupied_microusd"],
        "stream_body_state_observed": wire.get("response_body_state"),
        "semantic_status": "not_measured", "transport": "mock"}))
