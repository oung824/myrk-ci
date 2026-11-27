import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest

from myrk.conversation import ConversationRecord, Task, Turn
from tests.support.agent_acceptance import AcceptancePlan, LiveAuthorization, LiveGate, builtin_registry, prepare_live
from tests.support.agent_acceptance import formal_service
from tests.support.agent_acceptance.core import SharedControlError
from tests.support.agent_acceptance.formal import provider_config


@pytest.mark.parametrize("state", ["running", "stopped", "authorization_changed"])
def test_formal_service_saves_shutdown_evidence_without_reopening_shared_stop(
    prepared_root, tmp_path, monkeypatch, state,
):
    case_id, protocol = "desktop.handoff", "deepseek"
    registry = builtin_registry()
    bundle = prepare_live(AcceptancePlan(cases=[case_id], protocols=[protocol], mode="prepare-live"),
        root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="synthetic-shutdown", binding=bundle.binding,
        total_limit=1, case_limits={case_id: 1}, total_microusd=122880,
        case_microusd={case_id: 122880}, transport="mock")
    gate = LiveGate(prepared_root, registry, grant)
    ledger_path = prepared_root / "ledger.json"
    runtime = tmp_path / "runtime"
    records = tmp_path / "records"
    profile = provider_config(protocol)
    task = Task(goal="Keep the cancelled original.", scope=None, provider_config=profile)
    original = ConversationRecord(version=10, messages=[], turns=[
        Turn(prompt=task.goal, answer="", status="cancelled", task=task),
    ]).model_dump_json().encode("utf-8")
    stopped_ledger = None

    async def serve_saved_cancellation(directory, *_args, **_kwargs):
        nonlocal stopped_ledger
        record_path = directory / "conversation.json"
        record_path.write_bytes(original)
        if state != "running":
            gate.request_stop(scope="shared", reason="synthetic durable shared stop",
                source=record_path, case_id=case_id, protocol=protocol)
            stopped_ledger = ledger_path.read_bytes()
            with pytest.raises(SharedControlError, match="Live accounting is stopped"):
                gate.validate(case_id=case_id, protocol=protocol)
        if state == "authorization_changed":
            gate.authorization = grant.model_copy(update={"authorization_id": "different"})

    value = SimpleNamespace(case_id=case_id, protocol=protocol, grant=grant, runtime=runtime,
        provider_config=profile, run_id="synthetic-shutdown", scenario_root=None)
    monkeypatch.setenv("MYRK_ACCEPTANCE_AUTHORITY", str(tmp_path / "authority.json"))
    monkeypatch.setenv("MYRK_PROCESS_RECEIPT_DIRECTORY", str(tmp_path / "receipts"))
    monkeypatch.setenv("MYRK_ACCEPTANCE_ROLE", "receiver")
    monkeypatch.setenv("MYRK_DATA_DIRECTORY", str(records))
    monkeypatch.setattr(formal_service, "verify_authority", lambda _path: (value, gate))
    monkeypatch.setattr(formal_service, "write_process_receipt", lambda *_args: None)
    monkeypatch.setattr(formal_service.sys, "addaudithook", lambda _hook: None)
    monkeypatch.setattr(formal_service, "serve", serve_saved_cancellation)
    closed = []
    close = httpx2.AsyncClient.aclose

    async def observe_client_close(self):
        await close(self)
        closed.append(True)

    monkeypatch.setattr(httpx2.AsyncClient, "aclose", observe_client_close)

    if state == "authorization_changed":
        with pytest.raises(SharedControlError, match="authorization changed"):
            asyncio.run(formal_service.main())
    else:
        asyncio.run(formal_service.main())
        exchange = json.loads((runtime / "receiver" / "exchange.json").read_bytes())
        transport = json.loads((runtime / "receiver" / "transport.json").read_bytes())
        assert exchange["task_id"] == task.id and exchange["product_attempts"] == []
        assert transport["mock_http_attempts"] == transport["real_http_attempts"] == 0
    assert closed == [True]
    assert (records / "conversation.json").read_bytes() == original
    if stopped_ledger is not None:
        assert ledger_path.read_bytes() == stopped_ledger
    else:
        assert gate.accounting_report()["case_stops"]
    assert gate.accounting_report()["reserved_requests"] == 0
