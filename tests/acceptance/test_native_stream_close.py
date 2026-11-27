"""Closing a native HTTP body ends in-flight work but never releases its cost."""

import asyncio
from base64 import b64encode
from hashlib import sha256
import json
import os
from pathlib import Path

import httpx2
import pytest

from tests.support.agent_acceptance import (AcceptanceCase, AcceptancePlan, CaseRegistry,
    FrozenExecution, FrozenParentBudget, LiveAuthorization, LiveGate, SharedControlError, prepare_live)
from tests.support.agent_acceptance.recorded_response import (
    RecordedResponseStream, capture_response_metadata, response_evidence)


async def stream_boundary_only(context):
    raise AssertionError("This registry freezes stream input; no conversation execution is selected.")


@pytest.fixture
def stream_gate(prepared_root):
    source = Path(__file__)
    execution = FrozenExecution(execution_id="stream-close:synthetic", case_id="budget.stream-close",
        protocol="deepseek", scenario_id="stream-close", material_family_id="stream-close-control",
        material_variant="baseline", material_path=source, material_sha256=sha256(source.read_bytes()).hexdigest(),
        startpoint="isolated-stream-close", order=1, request_cap=8, cost_cap_microusd=983040)
    parent = FrozenParentBudget(parent_id="stream-close-" + prepared_root.name, accounting_root=prepared_root,
        request_cap=8, phase_microusd={"screening": 983040, "quality": 0, "judge": 0},
        reserve_microusd=0, profile="synthetic")
    registry = CaseRegistry()
    registry.register(AcceptanceCase(execution.case_id, "request-input", ("deepseek",), stream_boundary_only,
        bindings=(source,), frozen_material_input=True))
    bundle = prepare_live(AcceptancePlan(executions=(execution,), parent_budget=parent, mode="prepare-live"),
        root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id="synthetic-native-stream-close", binding=bundle.binding,
        parent_budget_id=parent.parent_id, transport="mock", total_limit=8,
        case_limits={execution.execution_id: 8}, total_microusd=983040,
        case_microusd={execution.execution_id: 983040})
    gate = LiveGate(prepared_root, registry, grant).for_execution(execution.execution_id)
    try:
        yield gate, execution
    finally:
        activation = gate.parent_binding_path(parent.parent_id)
        if activation.exists():
            activation.unlink()


@pytest.mark.parametrize("received_chunks", [0, 1, 2, None], ids=["before-body", "partial", "done-without-eof", "natural-eof"])
def test_native_stream_close_preserves_budget_and_stop(stream_gate, received_chunks):
    gate, execution = stream_gate
    chunks = [b'data: {"choices":[{"delta":{"content":"received"}}]}\n\n',
        b'data: [DONE]\n\n', b'data: {"choices":[]}\n\n']
    captures = []
    advanced = []

    class Source(httpx2.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                advanced.append(chunk)
                yield chunk

    async def consume():
        for number in range(1, 7):
            body = json.dumps({"stream": True, "messages": [{"role": "user", "content": str(number)}]}).encode()
            reserved = gate.reserve(case_id=execution.case_id, protocol="deepseek", actor="main", purpose="work",
                product_number=number, sdk_input_id="stream-source-" + str(number), request_sha256=sha256(body).hexdigest())
            gate.mark_http_started(reserved)
            record = {"status": 200, "received_utf8": "", "request_utf8": body.decode()}
            response = httpx2.Response(200, stream=Source(), headers={"content-type": "text/event-stream"})
            record["response_metadata"] = capture_response_metadata(response, source="fixed_fixture")
            response.stream = RecordedResponseStream(response.stream, record, lambda: None,
                lambda: gate.mark_response_complete(reserved, record),
                lambda reason: gate.mark_unknown(reserved, fault_origin=reason))
            if received_chunks is None:
                assert await response.aread() == b"".join(chunks)
            else:
                iterator = response.aiter_bytes()
                for _ in range(received_chunks):
                    await anext(iterator)
                await response.aclose()
                assert record.get("response_body_state") == "closed_before_eof"
            before = (gate.root / "ledger.json").read_bytes()
            await response.aclose()
            await response.stream.aclose()
            assert (gate.root / "ledger.json").read_bytes() == before
            expected = b"".join(chunks if received_chunks is None else chunks[:received_chunks])
            assert record["received_utf8"] == expected.decode()
            assert record.get("received_base64", "") == b64encode(expected).decode("ascii")
            original = json.dumps(record, sort_keys=True)
            evidence = response_evidence(record)
            assert json.dumps(record, sort_keys=True) == original
            assert evidence["status"] == ("decoded" if received_chunks is None else "incomplete")
            if received_chunks is None:
                assert "response_body_state" not in record
            else:
                assert evidence["text"] is None and evidence["events"] == []
            captures.append(record)

    asyncio.run(consume())
    assert advanced == (chunks if received_chunks is None else chunks[:received_chunks]) * 6
    report = gate.accounting_report()
    assert report["actual_http"] == report["reserved_requests"] == 6
    assert report["request_limit"] == 8 and report["cost_limit_microusd"] == 983040
    assert report["occupied_microusd"] == 88476
    assert all(row["occupied_microusd"] == row["reservation_microusd"] == 14746 for row in report["attempts"])
    assert all(row["state"] == ("response_complete" if received_chunks is None else "unknown") for row in report["attempts"])
    assert report["completed_responses"] == (6 if received_chunks is None else 0)
    gate.stop(reason="Synthetic stream-close boundary stop")
    with pytest.raises(SharedControlError):
        gate.reserve(case_id=execution.case_id, protocol="deepseek", actor="main", purpose="work",
            product_number=7, sdk_input_id="stopped", request_sha256="0" * 64)
    assert gate.accounting_report()["actual_http"] == 6
    output = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports" / "stream-close" / (gate.root.name + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"received_chunks": received_chunks, "captures": captures,
        "accounting": gate.accounting_report()}, indent=2) + "\n", encoding="utf-8")
