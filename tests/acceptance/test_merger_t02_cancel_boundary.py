"""Fresh merger counterexample through public product cancellation."""
import asyncio
import json
import pytest
from pydantic_ai import CancellationToken
from myrk.conversation import Conversation, Task
from tests.support.agent_acceptance import (AcceptancePlan, LiveAuthorization, LiveGate,
    AcceptanceCase, CaseOutcome, CaseRegistry, Reply, prepare_live, run_plan)

async def stopped_case(context):
    token = CancellationToken()
    session = context.session([Reply(text="A partial answer has arrived.")])
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Give one answer.", scope=None, provider_config=session.config)
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, task=task, model=model,
            cancellation_token=token, on_update=lambda event: token.cancel() if event.kind == "text" else None)
    exchange = session.save(context.root / "exchange.json", attempts=[a.model_dump(mode="json") for a in turn.request_attempts],
        run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    return CaseOutcome("passed" if turn.status == "cancelled" else "failed", {"turn_status": turn.status},
        {"conversation": conversation.path, "exchange": exchange})

def test_public_user_stop_is_durable_before_reopen(prepared_root, acceptance_public_root):
    registry = CaseRegistry()
    registry.register(AcceptanceCase("budget.user-stop", "request-input", ("deepseek",), stopped_case))
    bundle = prepare_live(AcceptancePlan(cases=["budget.user-stop"], mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="offline-public-user-stop", binding=bundle.binding, total_limit=2,
        case_limits={"budget.user-stop": 2}, total_microusd=29492, case_microusd={"budget.user-stop": 29492}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    result = asyncio.run(run_plan(AcceptancePlan(cases=["budget.user-stop"]), root=acceptance_public_root,
        registry=registry, live_gate=gate))
    before = gate.accounting_report()
    reopened = LiveGate(prepared_root, registry, grant)
    again = asyncio.run(run_plan(AcceptancePlan(cases=["budget.user-stop"]), root=acceptance_public_root / "reopen",
        registry=registry, live_gate=reopened))
    after = reopened.accounting_report()
    print(json.dumps({"turn_status": result.cases[0].facts["turn_status"],
        "first_state": before["attempts"][0]["state"], "before_http": before["actual_http"],
        "before_stops": before["case_stops"], "after_http": after["actual_http"],
        "after_stops": after["case_stops"], "execution_id_same": result.cases[0].execution_id == again.cases[0].execution_id}))
    assert result.cases[0].facts["turn_status"] == "cancelled"
    assert after["actual_http"] == before["actual_http"], "Reopening a stopped execution sent another HTTP request"
    assert "budget.user-stop:deepseek" in before["case_stops"]

    assert before["attempts"][0]["state"] == "unknown"
    assert before["occupied_microusd"] == after["occupied_microusd"] == 14746
    assert before["attempts"][0]["usage_estimate_microusd"] is None
    assert result.cases[0].execution_id == again.cases[0].execution_id


async def completed_case(context):
    session = context.session([Reply(text="A complete answer.")])
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Give one answer.", scope=None, provider_config=session.config)
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, task=task, model=model)
    exchange = session.save(context.root / "exchange.json", attempts=[a.model_dump(mode="json") for a in turn.request_attempts],
        run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    return CaseOutcome("passed" if turn.status == "completed" else "failed", {"turn_status": turn.status},
        {"conversation": conversation.path, "exchange": exchange})


def test_user_stop_keeps_independent_normal_stream_close_open(prepared_root, acceptance_public_root):
    registry = CaseRegistry()
    registry.register(AcceptanceCase("budget.user-stop", "request-input", ("deepseek",), stopped_case))
    registry.register(AcceptanceCase("budget.independent", "request-input", ("deepseek",), completed_case))
    names = ["budget.user-stop", "budget.independent"]
    bundle = prepare_live(AcceptancePlan(cases=names, mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="offline-public-independent", binding=bundle.binding, total_limit=3,
        case_limits={"budget.user-stop": 2, "budget.independent": 1}, total_microusd=44238,
        case_microusd={"budget.user-stop": 29492, "budget.independent": 14746}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    result = asyncio.run(run_plan(AcceptancePlan(cases=names), root=acceptance_public_root, registry=registry, live_gate=gate))
    assert result.cases[1].facts["turn_status"] == "completed"
    report = gate.accounting_report()
    assert report["actual_http"] == 2
    assert set(report["case_stops"]) == {"budget.user-stop:deepseek"}
    assert not report["stopped"]


async def stopped_before_send(context):
    token = CancellationToken()
    token.cancel()
    session = context.session([Reply(text="Must not be sent.")])
    conversation = Conversation(context.root / "conversation.json")
    task = Task(goal="Give one answer.", scope=None, provider_config=session.config)
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, task=task, model=model, cancellation_token=token)
    exchange = session.save(context.root / "exchange.json", attempts=[a.model_dump(mode="json") for a in turn.request_attempts],
        run_id=context.run_id, case_id=context.case_id, task_id=task.id)
    return CaseOutcome("passed" if turn.status == "cancelled" else "failed", {"turn_status": turn.status},
        {"conversation": conversation.path, "exchange": exchange})


def test_user_stop_before_any_http_also_keeps_execution_stopped(prepared_root, acceptance_public_root):
    registry = CaseRegistry()
    registry.register(AcceptanceCase("budget.before-send-stop", "request-input", ("deepseek",), stopped_before_send))
    name = "budget.before-send-stop"
    bundle = prepare_live(AcceptancePlan(cases=[name], mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="offline-before-send-stop", binding=bundle.binding, total_limit=1,
        case_limits={name: 1}, total_microusd=14746, case_microusd={name: 14746}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    result = asyncio.run(run_plan(AcceptancePlan(cases=[name]), root=acceptance_public_root, registry=registry, live_gate=gate))
    report = gate.accounting_report()
    assert result.cases[0].facts["turn_status"] == "cancelled"
    assert report["reserved_requests"] == report["actual_http"] == 0
    assert "budget.before-send-stop:deepseek" in report["case_stops"]
    asyncio.run(run_plan(AcceptancePlan(cases=[name]), root=acceptance_public_root / "reopen", registry=registry, live_gate=gate))
    assert gate.accounting_report()["actual_http"] == 0


def test_old_unrelated_cancelled_turn_does_not_stop_current_execution(prepared_root, acceptance_public_root):
    history_registry = CaseRegistry()
    history_registry.register(AcceptanceCase("budget.old-stop", "request-input", ("deepseek",), stopped_case))
    history = asyncio.run(run_plan(AcceptancePlan(cases=["budget.old-stop"]), root=acceptance_public_root / "history", registry=history_registry))
    original = history.cases[0].evidence["conversation"]

    async def current(context):
        path = context.root / "conversation.json"
        path.write_bytes(original.read_bytes())
        conversation = Conversation.open(path)
        session = context.session([Reply(text="Current task completed.")])
        task = Task(goal="Give one new answer.", scope=None, provider_config=session.config)
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, task=task, model=model)
        exchange = session.save(context.root / "exchange.json", attempts=[a.model_dump(mode="json") for a in turn.request_attempts],
            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
        return CaseOutcome("passed" if turn.status == "completed" else "failed", {"turn_status": turn.status},
            {"conversation": conversation.path, "exchange": exchange})

    registry = CaseRegistry()
    registry.register(AcceptanceCase("budget.current", "request-input", ("deepseek",), current))
    bundle = prepare_live(AcceptancePlan(cases=["budget.current"], mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="offline-old-history", binding=bundle.binding, total_limit=1,
        case_limits={"budget.current": 1}, total_microusd=14746, case_microusd={"budget.current": 14746}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    result = asyncio.run(run_plan(AcceptancePlan(cases=["budget.current"]), root=acceptance_public_root / "current", registry=registry, live_gate=gate))
    assert result.cases[0].facts["turn_status"] == "completed"
    assert gate.accounting_report()["actual_http"] == 1
    assert not gate.accounting_report()["case_stops"]



def test_missing_stop_original_closes_sending_before_reopen(prepared_root, acceptance_public_root):
    from tests.support.agent_acceptance import SharedControlError

    async def missing(context):
        outcome = await stopped_case(context)
        outcome.evidence["conversation"].unlink()
        return outcome

    registry = CaseRegistry()
    registry.register(AcceptanceCase("budget.missing-stop", "request-input", ("deepseek",), missing))
    name = "budget.missing-stop"
    bundle = prepare_live(AcceptancePlan(cases=[name], mode="prepare-live"), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="offline-missing-stop", binding=bundle.binding, total_limit=2,
        case_limits={name: 2}, total_microusd=29492, case_microusd={name: 29492}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    with pytest.raises(SharedControlError, match="stop original cannot be read"):
        asyncio.run(run_plan(AcceptancePlan(cases=[name]), root=acceptance_public_root, registry=registry, live_gate=gate))
    assert gate.accounting_report()["actual_http"] == 1
    assert gate.accounting_report()["occupied_microusd"] == 14746
    with pytest.raises(SharedControlError, match="stopped"):
        asyncio.run(run_plan(AcceptancePlan(cases=[name]), root=acceptance_public_root / "reopen", registry=registry, live_gate=gate))
    assert gate.accounting_report()["actual_http"] == 1
