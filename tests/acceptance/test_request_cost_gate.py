"""Single-execution grants constrain the existing terminal transport."""

import asyncio
import json

import pytest

from tests.support.agent_acceptance import (AcceptancePlan, LiveAuthorization, LiveGate, SharedControlError,
    AcceptanceCase, CaseOutcome, CaseRegistry, Reply, builtin_registry, prepare_live, run_plan)


def test_amount_exhaustion_stops_before_second_http_with_requests_left(prepared_root, acceptance_public_root):
    registry = builtin_registry()
    bundle = prepare_live(AcceptancePlan(cases=["ordinary.attachment"], mode="prepare-live"),
        root=prepared_root, registry=registry,
        budget_proposals={"ordinary.attachment": {"request_limit": 2, "cost_limit_microusd": 29492}})
    grant = LiveAuthorization(authorization_id="offline-money-boundary", binding=bundle.binding,
        total_limit=2, case_limits={"ordinary.attachment": 2}, total_microusd=14746,
        case_microusd={"ordinary.attachment": 14746}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    result = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.attachment"]), root=acceptance_public_root,
        registry=registry, live_gate=gate))
    report = gate.accounting_report()
    assert report["reserved_requests"] == 1
    assert report["actual_http"] == 1
    assert report["occupied_microusd"] == 14746
    assert report["case_stops"]["ordinary.attachment:deepseek"]["reason"] == "cost budget exhausted"
    assert result.cases[0].facts["budget"]["unfinished"] == "Remaining work was not authorized to send."
    assert result.cases[0].semantic_status == "not_measured"
    assert json.loads(result.cases[0].evidence["budget.ledger"].read_bytes())["attempts"][0]["execution_id"] == result.cases[0].execution_id


def test_not_sent_releases_only_money_and_retry_has_its_own_identity(prepared_root, acceptance_public_root):
    from myrk.conversation import Conversation, Task

    async def retry_case(context):
        session = context.session([Reply(text="not sent", not_sent=True), Reply(text="The result is 42.")])
        conversation = Conversation(context.root / "conversation.json")
        task = Task(goal="What is six times seven?", scope=None, provider_config=session.config)
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, task=task, model=model)
        exchange = session.save(context.root / "exchange.json", attempts=[a.model_dump(mode="json") for a in turn.request_attempts],
            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
        return CaseOutcome("passed" if turn.status == "completed" else "failed", {"turn_status": turn.status},
            {"conversation": conversation.path, "exchange": exchange})

    registry = CaseRegistry()
    registry.register(AcceptanceCase("budget.retry", "request-input", ("deepseek",), retry_case))
    bundle = prepare_live(AcceptancePlan(cases=["budget.retry"], mode="prepare-live"),
        root=prepared_root, registry=registry,
        budget_proposals={"budget.retry": {"request_limit": 2, "cost_limit_microusd": 29492}})
    grant = LiveAuthorization(authorization_id="offline-not-sent", binding=bundle.binding, total_limit=2,
        case_limits={"budget.retry": 2}, total_microusd=14746, case_microusd={"budget.retry": 14746}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    result = asyncio.run(run_plan(AcceptancePlan(cases=["budget.retry"]), root=acceptance_public_root,
        registry=registry, live_gate=gate))
    report = LiveGate(prepared_root, registry, grant).accounting_report()
    assert report["reserved_requests"] == 2
    assert report["not_sent"] == 1
    assert report["actual_http"] == 1
    assert report["occupied_microusd"] == 14746
    assert [a["product_number"] for a in report["attempts"]] == [1, 2]
    assert report["attempts"][0]["fault_origin"].startswith("planned local pre-send")
    assert report["attempts"][0]["supplier_final_microusd"] is None
    assert report["attempts"][1]["usage_estimate_microusd"] == 15
    assert result.cases[0].facts["turn_status"] == "completed"


def test_timeout_keeps_full_unresolved_amount_and_retry_reserves_again(prepared_root, acceptance_public_root):
    from myrk.conversation import Conversation, Task

    async def timeout_case(context):
        session = context.session([Reply(timeout=True), Reply(text="The result is 42.")])
        conversation = Conversation(context.root / "conversation.json")
        task = Task(goal="What is six times seven?", scope=None, provider_config=session.config)
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, task=task, model=model)
        exchange = session.save(context.root / "exchange.json", attempts=[a.model_dump(mode="json") for a in turn.request_attempts],
            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
        return CaseOutcome("passed" if turn.status == "completed" else "failed", {"turn_status": turn.status},
            {"conversation": conversation.path, "exchange": exchange})

    registry = CaseRegistry()
    registry.register(AcceptanceCase("budget.timeout", "request-input", ("deepseek",), timeout_case))
    bundle = prepare_live(AcceptancePlan(cases=["budget.timeout"], mode="prepare-live"),
        root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="offline-timeout", binding=bundle.binding, total_limit=2,
        case_limits={"budget.timeout": 2}, total_microusd=29492, case_microusd={"budget.timeout": 29492}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    asyncio.run(run_plan(AcceptancePlan(cases=["budget.timeout"]), root=acceptance_public_root,
        registry=registry, live_gate=gate))
    report = gate.accounting_report()
    assert report["reserved_requests"] == report["actual_http"] == 2
    assert report["occupied_microusd"] == 29492
    assert report["attempts"][0]["state"] == "unknown"
    assert report["attempts"][0]["usage_estimate_microusd"] is None
    assert report["attempts"][1]["usage_estimate_microusd"] == 15
    assert report["unknown_fee_requests"] == 2


@pytest.mark.parametrize("damage", ["occupied", "execution", "price", "missing", "authorization", "not-sent-proof"])
def test_damaged_or_rebound_ledger_cannot_send_after_reopen(prepared_root, acceptance_public_root, damage):
    registry = builtin_registry()
    bundle = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"),
        root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="offline-original-grant", binding=bundle.binding,
        total_limit=2, case_limits={"ordinary.question": 2}, total_microusd=29492,
        case_microusd={"ordinary.question": 29492}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=acceptance_public_root,
        registry=registry, live_gate=gate))
    path = prepared_root / "ledger.json"
    saved = json.loads(path.read_bytes())
    if damage == "missing":
        path.unlink()
    elif damage == "authorization":
        grant = grant.model_copy(update={"authorization_id": "replacement"})
    elif damage == "not-sent-proof":
        saved["attempts"][0].update(state="not_sent", occupied_microusd=0, fault_origin="unverified replacement")
        path.write_text(json.dumps(saved), encoding="utf-8")
    else:
        field, value = {"occupied": ("occupied_microusd", 0), "execution": ("execution_id", "replacement"),
                        "price": ("reservation_microusd", 1)}[damage]
        saved["attempts"][0][field] = value
        path.write_text(json.dumps(saved), encoding="utf-8")
    original = path.read_bytes() if path.exists() else None
    with pytest.raises(SharedControlError, match="Prepared execution binding cannot be read" if damage == "missing" else "accounting|authorization") as refusal:
        asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=acceptance_public_root / "reopen",
            registry=registry, live_gate=LiveGate(prepared_root, registry, grant)))
    assert (path.read_bytes() if path.exists() else None) == original
    if damage == "missing":
        assert isinstance(refusal.value.__cause__, FileNotFoundError)
        assert refusal.value.__cause__.filename == str(path)
        assert not (acceptance_public_root / "reopen").exists()


@pytest.mark.parametrize("case_id,expected", [
    ("delegation.review-retry", {"main": 2, "delegation": 2, "action_review": 2}),
    ("memory.review-add", {"main": 4, "memory_review": 2}),
    ("compaction.short", {"main": 3}),
])
def test_all_product_roles_and_retry_steps_spend_the_same_execution_grant(prepared_root, acceptance_public_root, case_id, expected):
    registry = builtin_registry()
    bundle = prepare_live(AcceptancePlan(cases=[case_id], mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="offline-role-account", binding=bundle.binding, total_limit=30,
        case_limits={case_id: 30}, total_microusd=442380, case_microusd={case_id: 442380}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    result = asyncio.run(run_plan(AcceptancePlan(cases=[case_id]), root=acceptance_public_root,
        registry=registry, live_gate=gate))
    report = gate.accounting_report()
    assert report["role_usage"] == expected
    assert report["reserved_requests"] == report["actual_http"] == sum(expected.values())
    assert report["occupied_microusd"] == sum(expected.values()) * 14746
    attempts = report["attempts"]
    assert all(item["sdk_input_id"] and item["request_sha256"] for item in attempts)
    assert all(item["execution_id"] == result.cases[0].execution_id for item in attempts)
    exchange = json.loads(result.cases[0].evidence["exchange"].read_bytes())
    originals = {item["number"]: item for item in exchange["product_attempts"]}
    assert all(item["product_step_id"] == originals[item["product_number"]].get("step_id") for item in attempts)
    if case_id == "delegation.review-retry":
        review = [item for item in attempts if item["actor"] == "action_review"]
        assert review[0]["product_step_id"] is review[1]["product_step_id"] is None
        assert review[0]["product_number"] != review[1]["product_number"]
    if case_id == "compaction.short":
        assert attempts[0]["purpose"] == "compaction"
    assert result.cases[0].product_status == "passed", result.cases[0].facts


@pytest.mark.parametrize("case_id,protocol,expected_state", [
    ("ordinary.missing-usage", "deepseek", "response_complete"),
    ("ordinary.question", "google", "response_complete"),
    ("ordinary.interrupted", "deepseek", "unknown"),
])
def test_missing_partial_or_interrupted_usage_keeps_full_amount(prepared_root, acceptance_public_root, case_id, protocol, expected_state):
    registry = builtin_registry()
    bundle = prepare_live(AcceptancePlan(cases=[case_id], protocols=[protocol], mode="prepare-live"),
        root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="offline-unknown-usage", binding=bundle.binding, total_limit=1,
        case_limits={case_id: 1}, total_microusd=20071, case_microusd={case_id: 20071}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    asyncio.run(run_plan(AcceptancePlan(cases=[case_id], protocols=[protocol]), root=acceptance_public_root,
        registry=registry, live_gate=gate))
    attempt = gate.accounting_report()["attempts"][0]
    assert attempt["state"] == expected_state
    assert attempt["occupied_microusd"] == (20071 if protocol == "google" else 14746)
    assert attempt["usage_estimate_microusd"] is None
    assert attempt["supplier_final_microusd"] is None


def test_cancelled_terminal_http_remains_unknown_and_fully_occupied(prepared_root, acceptance_public_root):
    from myrk.conversation import Conversation, Task

    async def cancelled(context):
        session = context.session([Reply(cancel=True)])
        conversation = Conversation(context.root / "conversation.json")
        task = Task(goal="Give one answer.", scope=None, provider_config=session.config)
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, task=task, model=model)
        exchange = session.save(context.root / "exchange.json", attempts=[a.model_dump(mode="json") for a in turn.request_attempts],
            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
        return CaseOutcome("inconclusive", {"turn_status": turn.status}, {"conversation": conversation.path, "exchange": exchange})

    registry = CaseRegistry()
    registry.register(AcceptanceCase("budget.cancel", "request-input", ("deepseek",), cancelled))
    bundle = prepare_live(AcceptancePlan(cases=["budget.cancel"], mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="offline-cancel", binding=bundle.binding, total_limit=1,
        case_limits={"budget.cancel": 1}, total_microusd=14746, case_microusd={"budget.cancel": 14746}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_plan(AcceptancePlan(cases=["budget.cancel"]), root=acceptance_public_root, registry=registry, live_gate=gate))
    report = gate.accounting_report()
    assert report["actual_http"] == 1
    assert report["attempts"][0]["state"] == "unknown"
    assert report["occupied_microusd"] == 14746
    assert "budget.cancel:deepseek" in report["case_stops"]
    reopened = LiveGate(prepared_root, registry, grant)
    asyncio.run(run_plan(AcceptancePlan(cases=["budget.cancel"]), root=acceptance_public_root / "reopen",
        registry=registry, live_gate=reopened))
    assert reopened.accounting_report()["actual_http"] == 1


def test_frozen_integer_quotes_match_specification_without_qualifying_routes(prepared_root):
    registry = builtin_registry()
    bundle = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"), root=prepared_root, registry=registry)
    quotes = {(price.protocol, price.role): price.reservation_microusd for price in bundle.prices}
    assert quotes["deepseek", "product"] == 14746
    assert quotes["google", "product"] == 20071
    assert quotes["google", "judge"] == 122880
    assert all(price.synthetic_only for price in bundle.prices)
    assert all(not route.sending_enabled and route.status == "unqualified" for route in bundle.eligibility)


@pytest.mark.parametrize("damage", ["missing-money", "request-proposal", "money-proposal"])
def test_explicit_authority_cannot_exceed_frozen_single_case_proposal(prepared_root, acceptance_public_root, damage):
    from tests.support.agent_acceptance import SharedControlError

    registry = builtin_registry()
    bundle = prepare_live(AcceptancePlan(cases=["ordinary.question"], mode="prepare-live"), root=prepared_root, registry=registry,
        budget_proposals={"ordinary.question": {"request_limit": 1, "cost_limit_microusd": 14746}})
    fields = dict(authorization_id="offline-explicit-cap", binding=bundle.binding, total_limit=1,
        case_limits={"ordinary.question": 1}, total_microusd=14746, case_microusd={"ordinary.question": 14746}, transport="mock")
    if damage == "missing-money":
        fields.pop("total_microusd")
        fields.pop("case_microusd")
    elif damage == "request-proposal":
        fields.update(total_limit=2, case_limits={"ordinary.question": 2})
    else:
        fields.update(total_microusd=14747, case_microusd={"ordinary.question": 14747})
    grant = LiveAuthorization.model_validate(fields)
    original = (prepared_root / "ledger.json").read_bytes()
    with pytest.raises(SharedControlError, match="authorization|exceeds"):
        asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.question"]), root=acceptance_public_root,
            registry=registry, live_gate=LiveGate(prepared_root, registry, grant)))
    assert (prepared_root / "ledger.json").read_bytes() == original
    assert not bundle.sending_enabled


def test_request_stop_preserves_independent_case_and_stays_stopped_after_reopen(prepared_root, acceptance_public_root):
    registry = builtin_registry()
    names = ["ordinary.attachment", "ordinary.question"]
    bundle = prepare_live(AcceptancePlan(cases=names, mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="offline-independent-cases", binding=bundle.binding, total_limit=3,
        case_limits={"ordinary.attachment": 1, "ordinary.question": 2}, total_microusd=44238,
        case_microusd={"ordinary.attachment": 29492, "ordinary.question": 14746}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    result = asyncio.run(run_plan(AcceptancePlan(cases=names), root=acceptance_public_root, registry=registry, live_gate=gate))
    assert gate.accounting_report()["case_stops"]["ordinary.attachment:deepseek"]["reason"] == "request budget exhausted"
    assert result.cases[1].product_status == "passed"
    assert result.cases[1].facts["budget"]["actual_http"] == 2
    assert not gate.accounting_report()["stopped"]
    reopened = LiveGate(prepared_root, registry, grant)
    again = asyncio.run(run_plan(AcceptancePlan(cases=["ordinary.attachment"]), root=acceptance_public_root / "reopen",
        registry=registry, live_gate=reopened))
    assert again.cases[0].execution_id == result.cases[0].execution_id
    assert reopened.accounting_report()["actual_http"] == 2
    assert reopened.accounting_report()["reserved_requests"] == 2
