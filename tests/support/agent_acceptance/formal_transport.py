"""Authorize and record terminal HTTP requests for formal acceptance."""

import asyncio
from collections.abc import Callable
from hashlib import sha256
import json
from pathlib import Path
from typing import TypedDict

import httpx2

from myrk.request_observation import current_request_attempt
from tests.support.agent_acceptance.core import SharedControlError
from tests.support.agent_acceptance.preparation import CaseBudgetExceeded, LiveGate
from tests.support.agent_acceptance.recorded_response import RecordedResponseStream, capture_response_metadata
from tests.support.agent_acceptance.replay import OfflineSession, active_sdk_input, active_judge_call, matches_active_sdk_input


class _TransportObservation(TypedDict):
    transport_kind: str
    mock_http_attempts: int
    real_http_attempts: int
    blocked_unplanned_http_attempts: int
    control_error: str | None
    control_error_history: list[str]
    planned_local_transport_failures: int


class _AuthorizedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, gate: LiveGate, case_id: str, session: OfflineSession, role: str, transport: httpx2.AsyncBaseTransport,
                 persist: Callable[[], None], local_failure: Callable[[], bool], *,
                 fault_origin: str = "planned local pre-send failure after the real completed check", product_records_root: Path | None = None, material_root: Path | None = None, hold_stop_response: bool = False) -> None:
        self.gate, self.case_id, self.session, self.role, self.transport = gate, case_id, session, role, transport
        if gate.authorization is not None and gate.authorization.transport == "mock" and type(transport) is not httpx2.MockTransport:
            raise SharedControlError("Mock authorization cannot acquire a real terminal HTTP transport.")
        self.persist = persist
        self.local_failure = local_failure
        self.fault_origin = fault_origin
        self.product_records_root = product_records_root
        self.material_root = material_root
        self.hold_stop_response = hold_stop_response
        self.observation: _TransportObservation = {"transport_kind": type(transport).__name__, "mock_http_attempts": 0,
            "real_http_attempts": 0, "blocked_unplanned_http_attempts": 0, "control_error": None, "control_error_history": [],
            "planned_local_transport_failures": 0}

    def stop_control(self, error: SharedControlError) -> None:
        self.observation["control_error_history"].append(str(error))
        self.observation["control_error"] = str(error)
        self.persist()
        try:
            self.gate.stop(reason=str(error))
        except SharedControlError as stop_error:
            self.observation["control_error"] = str(error) + "; durable stop failed: " + str(stop_error)
            self.persist()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if self.role != "receiver" or request.method != "POST":
            self.observation["blocked_unplanned_http_attempts"] += 1
            error = SharedControlError("Only the authorized receiver may send a model request; catalog HTTP is closed.")
            self.stop_control(error)
            raise error
        attempt = current_request_attempt()
        judge = active_judge_call.get()
        try:
            if (attempt is None) == (judge is None):
                raise SharedControlError("A formal HTTP request requires one actual product RequestAttempt or SDK-owned judge call.")
            if judge is not None and judge.execution_id != self.gate.bound_execution_id:
                raise SharedControlError("Judge SDK call is outside its frozen execution identity.")
            assert judge is not None or attempt is not None
            identity = judge.request_identity if judge is not None else {
                name: getattr(attempt, name) for name in ("number", "actor", "purpose", "delegation_id")}
            sdk_id = active_sdk_input.get()
            matching_inputs = [row for row in self.session.sdk_inputs if row.get("sdk_input_id") == sdk_id]
            if not sdk_id or len(matching_inputs) != 1:
                raise SharedControlError("A formal HTTP attempt lacks its unique complete observed SDK input; no network send occurred.")
            sdk = matching_inputs[0]
            if sdk.get("request_identity") != identity or (judge is not None and judge.call_id != sdk_id):
                raise SharedControlError("A formal HTTP attempt does not match its observed SDK input owner.")
            if not matches_active_sdk_input(sdk):
                raise SharedControlError("A formal HTTP attempt lacks its intact observed SDK input; no network send occurred.")
        except SharedControlError as error:
            self.stop_control(error)
            raise
        self.persist()
        try:
            if self.material_root is None:
                self.gate.inspect_frozen_material(request)
            else:
                self.gate.inspect_frozen_material(request, material_root=self.material_root)
            if self.case_id.startswith("quality.") and attempt is not None:
                if self.material_root is None or self.product_records_root is None:
                    raise SharedControlError("Quality native permission check requires its actual scenario and product record roots.")
                from tests.support.agent_acceptance.quality_runtime import verify_product_permissions
                verify_product_permissions(self.case_id, self.material_root, self.session.protocol, self.product_records_root, attempt)
            eligibility = self.gate.inspect_native_request(request, protocol=self.session.protocol,
                role="judge" if judge is not None else "product", config=self.session.config)
            if self.gate.authorization is not None and self.gate.authorization.transport == "real":
                raise SharedControlError("Paid route unqualified at terminal HTTP: " + ", ".join(eligibility.unresolved))
            number = self.gate.reserve(case_id=self.case_id, protocol=self.session.protocol, actor=identity["actor"],
                purpose=identity["purpose"], delegation_id=identity["delegation_id"], product_number=identity["number"],
                role="judge" if judge is not None else "product", product_step_id=attempt.step_id if attempt is not None else None,
                sdk_input_id=sdk_id, request_sha256=sha256(request.content).hexdigest(),judge_call_id=judge.call_id if judge is not None else None,
                product_records_root=self.product_records_root)
        except CaseBudgetExceeded:
            raise
        except SharedControlError as error:
            self.stop_control(error)
            raise
        record = {"attempt": len(self.session.exchanges) + 1, "method": request.method,
            "url": str(request.url), "request_utf8": request.content.decode("utf-8"), "received_utf8": "",
            "ledger_number": number, "sdk_input_id": sdk_id,
            "transport_provenance":"fixed_fixture" if type(self.transport) is httpx2.MockTransport else "real_http",
            "billing_eligibility": eligibility.model_dump(mode="json"),
            "request_identity": identity}
        self.session.exchanges.append(record)
        if self.local_failure():
            self.observation["planned_local_transport_failures"] += 1
            record.update(not_sent=True, fault_origin=self.fault_origin,
                error="ReadError: controlled local pre-send interruption; no terminal HTTP was invoked", status=None)
            self.gate.mark_not_sent(number, fault_origin=self.fault_origin)
            self.persist()
            raise httpx2.ReadError("controlled local pre-send interruption; no terminal HTTP was invoked", request=request)
        try:
            self.gate.mark_http_started(number)
        except CaseBudgetExceeded:
            origin = "accepted durable stop before terminal HTTP"
            record.update(not_sent=True,fault_origin=origin,status=None)
            self.gate.mark_not_sent(number,fault_origin=origin)
            self.persist()
            raise
        if self.gate.authorization is not None and self.gate.authorization.transport == "mock":
            self.observation["mock_http_attempts"] += 1
        else:
            self.observation["real_http_attempts"] += 1
        if self.case_id == "quality.qg3.formal.9" and attempt is not None:
            record["terminal_http_started"] = True
            record["product_step_id"] = attempt.step_id
        self.persist()
        try:
            response = await self.transport.handle_async_request(request)
            if self.case_id == "quality.qg2.formal.10" and response.status_code == 200:
                assert self.material_root is not None and self.product_records_root is not None
                from tests.support.agent_acceptance.quality_files import apply_external_conflict
                from tests.support.agent_acceptance.quality_runtime import bound_task
                try:
                    _, visible, _ = bound_task(self.case_id, self.material_root)
                    records = [json.loads(path.read_bytes()) for path in self.product_records_root.glob("*.json")]
                    apply_external_conflict(visible, self.material_root, records, sdk)
                except BaseException:
                    await response.aclose()
                    raise
            if self.hold_stop_response:
                assert self.material_root is not None and self.product_records_root is not None
                from tests.support.agent_acceptance.quality_recovery_resume import hold_stop_checkpoint
                try:
                    await hold_stop_checkpoint(self.material_root, self.product_records_root, sdk_id)
                except BaseException:
                    await response.aclose()
                    raise
        except SharedControlError as error:
            record["error"] = f"{type(error).__name__}: {error}"
            self.stop_control(error)
            raise
        except (httpx2.HTTPError, asyncio.CancelledError) as error:
            self.gate.mark_unknown(number, fault_origin=f"{type(error).__name__}: {error}",
                stop_case=isinstance(error, asyncio.CancelledError))
            record["error"] = f"{type(error).__name__}: {error}"
            self.persist()
            raise
        record["ledger_number"] = number
        record["status"] = response.status_code
        record["response_metadata"] = capture_response_metadata(response,
            source="fixed_fixture" if type(self.transport) is httpx2.MockTransport else "observed_headers")
        if self.gate.accepted_stop(case_id=self.case_id,protocol=self.session.protocol,request_number=number):
            record["response_body_state"] = "cancelled_before_delivery"
            self.gate.mark_unknown(number,fault_origin="accepted stop before response delivery",stop_case=True)
            self.persist()
            await response.aclose()
            raise asyncio.CancelledError("accepted stop before response delivery")
        self.persist()
        assert isinstance(response.stream, httpx2.AsyncByteStream)
        response.stream = RecordedResponseStream(response.stream, record, self.persist,
            lambda: self.gate.mark_response_complete(number, record),
            lambda reason: self.gate.mark_unknown(number, fault_origin=reason, stop_case=reason == "cancelled during response body"))
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()
