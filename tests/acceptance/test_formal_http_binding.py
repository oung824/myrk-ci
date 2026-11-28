import asyncio
from collections.abc import Awaitable, Callable
from hashlib import sha256
import json
from pathlib import Path

import httpx2
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
import pytest

from myrk.conversation import Conversation, Task
from myrk.request_observation import current_request_attempt
from tests.support.agent_acceptance import (
    AcceptanceCase,
    AcceptancePlan,
    CaseContext,
    CaseOutcome,
    CaseRegistry,
    LiveAuthorization,
    LiveGate,
    OfflineSession,
    Reply,
    SharedControlError,
    prepare_live,
    run_plan,
)
from tests.support.agent_acceptance.replay import JudgeCall, SDKCapture


CASE_ID = "control.http-binding"
SENTINEL_ID = "control.http-binding-sentinel"
PROMPT = "Return the fixed answer from the synthetic terminal."


def mock_plan(case_run: Callable[[CaseContext], Awaitable[CaseOutcome]],
              prepared_root: Path, *, sentinel: Callable[[CaseContext], Awaitable[CaseOutcome]] | None = None,
              ) -> tuple[AcceptancePlan, CaseRegistry, LiveGate]:
    registry = CaseRegistry()
    registry.register(AcceptanceCase(CASE_ID, "control", ("deepseek",), case_run))
    if sentinel is not None:
        registry.register(AcceptanceCase(SENTINEL_ID, "control", ("deepseek",), sentinel))
    plan = AcceptancePlan(cases=list(registry.cases))
    bundle = prepare_live(plan.model_copy(update={"mode": "prepare-live"}), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="synthetic-http-binding", binding=bundle.binding,
        total_limit=len(plan.cases), case_limits=dict.fromkeys(plan.cases, 1), total_microusd=len(plan.cases) * 122880,
        case_microusd=dict.fromkeys(plan.cases, 122880), transport="mock")
    return plan, registry, LiveGate(prepared_root, registry, grant)


@pytest.mark.parametrize("fault", [
    "missing_owner", "conflicting_owner", "missing_sdk", "duplicate_sdk", "incomplete_sdk", "wrong_sdk_owner",
    "empty_sdk_messages", "empty_sdk_parameters",
])
def test_formal_http_refuses_unbound_input_before_mock_send_or_reservation(
    fault, monkeypatch, prepared_root, acceptance_public_root,
):
    fixture = OfflineSession("deepseek", [Reply(text="Fixed answer.")])
    if fault == "conflicting_owner":
        monkeypatch.setattr(SDKCapture, "judge_call",
            lambda _model, identity: JudgeCall(identity, "unexpected-judge-execution", "scoring"))
    elif fault != "missing_owner":
        capture = SDKCapture.capture

        def damaged_capture(model, messages, settings, parameters):
            identity = capture(model, messages, settings, parameters)
            if fault == "missing_sdk":
                model.inputs.pop()
            elif fault == "duplicate_sdk":
                model.inputs.append(model.inputs[-1].copy())
            elif fault == "incomplete_sdk":
                del model.inputs[-1]["parameters"]
            elif fault == "empty_sdk_messages":
                model.inputs[-1]["messages"] = []
            elif fault == "empty_sdk_parameters":
                model.inputs[-1]["parameters"] = {}
            elif fault == "wrong_sdk_owner":
                model.inputs[-1]["request_identity"] = {**model.inputs[-1]["request_identity"], "number": 99}
            return identity

        monkeypatch.setattr(SDKCapture, "capture", damaged_capture)

    async def exercise(context: CaseContext) -> CaseOutcome:
        session = context.session(fixture.replies, transport=httpx2.MockTransport(fixture.respond))
        async with session.model() as model:
            if fault == "missing_owner":
                await model.request([ModelRequest(parts=[UserPromptPart(PROMPT)])], None, ModelRequestParameters())
            else:
                conversation = Conversation(context.root / "conversation.json")
                task = Task(goal=PROMPT, scope=None, provider_config=session.config)
                await conversation.ask(PROMPT, document=None, task=task, model=model)
        return CaseOutcome("passed")

    plan, registry, gate = mock_plan(exercise, prepared_root)
    failure = None
    try:
        asyncio.run(run_plan(plan, root=acceptance_public_root, registry=registry, live_gate=gate))
    except SharedControlError as error:
        failure = error
    report = gate.accounting_report()
    assert fixture.exchanges == []
    assert report["reserved_requests"] == report["actual_http"] == 0
    assert report["attempts"] == []
    assert report["stopped"] is True
    assert failure is not None
    reason = "one actual product RequestAttempt" if fault in {"missing_owner", "conflicting_owner"} else "observed SDK input"
    assert reason in str(failure)


@pytest.mark.parametrize("summary", ["intact", "missing", "mismatch", "missing_with_sdk_conflict"])
def test_formal_evidence_checks_product_summary_against_actual_terminal_send(
    summary, prepared_root, acceptance_public_root,
):
    fixture = OfflineSession("deepseek", [Reply(text="Fixed answer.")])
    terminal_bodies: list[bytes] = []
    sentinel_runs: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        terminal_bodies.append(request.content)
        attempt = current_request_attempt()
        assert attempt is not None and attempt.observation is not None and attempt.observation.wire is not None
        assert attempt.observation.wire.body_sha256 == sha256(request.content).hexdigest()
        if summary in {"missing", "missing_with_sdk_conflict"}:
            attempt.observation.wire = None
        elif summary == "mismatch":
            attempt.observation.wire.body_sha256 = "0" * 64
        return fixture.respond(request)

    async def exercise(context: CaseContext) -> CaseOutcome:
        session = context.session(fixture.replies, transport=httpx2.MockTransport(respond))
        conversation = Conversation(context.root / "conversation.json")
        task = Task(goal=PROMPT, scope=None, provider_config=session.config)
        async with session.model() as model:
            turn = await conversation.ask(PROMPT, document=None, task=task, model=model)
        assert turn.status == "completed" and turn.answer == "Fixed answer."
        if summary == "missing_with_sdk_conflict":
            sdk_identity = session.sdk_inputs[-1]["request_identity"]
            session.sdk_inputs[-1]["request_identity"] = {**sdk_identity, "number": 99}
        saved = json.loads(conversation.path.read_bytes())
        exchange = session.save(context.root / "exchange.json", attempts=saved["turns"][-1]["request_attempts"],
            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
        return CaseOutcome("passed", evidence={"conversation": conversation.path, "exchange": exchange})

    async def sentinel(context: CaseContext) -> CaseOutcome:
        sentinel_runs.append(context.case_id)
        return CaseOutcome("passed")

    plan, registry, gate = mock_plan(
        exercise, prepared_root, sentinel=sentinel if summary == "missing_with_sdk_conflict" else None,
    )
    run = run_plan(plan, root=acceptance_public_root, registry=registry, live_gate=gate)
    case_root = acceptance_public_root / CASE_ID / "deepseek"
    if summary == "intact":
        outcome, = asyncio.run(run).cases
        assert outcome.product_status == outcome.tool_status == "passed"
        assert outcome.facts["mock_http_attempts"] == 1 and outcome.facts["real_http_attempts"] == 0
    else:
        reason = "HTTP evidence has no product attempt: exchange"
        with pytest.raises(SharedControlError, match=reason):
            asyncio.run(run)
        failed, *remaining = json.loads((acceptance_public_root / "result.json").read_bytes())["cases"]
        assert failed["product_status"] == failed["tool_status"] == "failed"
        assert failed["facts"] == {"error": reason}
        assert (case_root / "control-failure.txt").read_text(encoding="utf-8").strip() == reason
        if summary == "missing_with_sdk_conflict":
            skipped, = remaining
            assert skipped["case_id"] == SENTINEL_ID and skipped["product_status"] == "not_executed"
            assert sentinel_runs == []
        else:
            assert remaining == []

    body, = terminal_bodies
    assert len(fixture.exchanges) == 1
    original = json.loads((case_root / "exchange.json").read_bytes())
    request, = original["exchanges"]
    sdk, = original["sdk_inputs"]
    product, = original["product_attempts"]
    saved = json.loads((case_root / "conversation.json").read_bytes())
    assert saved["turns"][-1]["request_attempts"] == [product]
    assert request["request_utf8"].encode("utf-8") == body and request["status"] == 200
    assert request["transport_provenance"] == "fixed_fixture" and "Fixed answer." in request["received_utf8"]
    assert product["status"] == "completed" and PROMPT in json.dumps(sdk["messages"])
    assert "settings" in sdk and isinstance(sdk["parameters"], dict)
    report = gate.accounting_report()
    assert report["reserved_requests"] == report["actual_http"] == report["completed_responses"] == 1
    archived_ledger = json.loads((case_root / "budget/ledger.json").read_bytes())
    assert archived_ledger["attempts"] == report["attempts"]
    reservation, = archived_ledger["attempts"]
    assert reservation["state"] == "response_complete"
    assert reservation["number"] == request["ledger_number"] == 1
    assert reservation["sdk_input_id"] == request["sdk_input_id"] == sdk["sdk_input_id"]
    assert reservation["request_sha256"] == sha256(body).hexdigest()
    assert request["request_identity"] == {
        "number": product["number"], "actor": product["actor"],
        "purpose": product["purpose"], "delegation_id": product["delegation_id"],
    }
    if summary == "missing_with_sdk_conflict":
        assert sdk["request_identity"] == {**request["request_identity"], "number": 99}
        assert report["stopped"] is True
    else:
        assert sdk["request_identity"] == request["request_identity"]
    assert reservation["product_number"] == product["number"]
    assert all(reservation[key] == product[key] for key in ("actor", "purpose", "delegation_id"))
    if summary in {"missing", "missing_with_sdk_conflict"}:
        assert product["observation"]["wire"] is None
    elif summary == "mismatch":
        assert product["observation"]["wire"]["body_sha256"] == "0" * 64
    else:
        assert product["observation"]["wire"]["body_sha256"] == reservation["request_sha256"]
