"""Formal desktop service with an explicit terminal-only qualification transport."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, nullcontext
import json
import os
import sys
from hashlib import sha256
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch

import httpx2
from pydantic_ai import models
from pydantic_ai.models import Model

from myrk import desktop_service
from myrk.provider import deepseek_model
from myrk.request_observation import current_request_attempt
from tests.support.agent_acceptance.core import SharedControlError
from tests.support.agent_acceptance.preparation import CaseBudgetExceeded, LiveGate
from tests.support.agent_acceptance.replay import OfflineSession, Reply, SDKCapture, active_sdk_input, active_judge_call
from tests.support.agent_acceptance.formal import verify_authority
from tests.support.agent_acceptance.formal_adoption import adoption_mock
from tests.support.agent_acceptance.save_diagnostics import observe_saves
from tests.support.agent_acceptance.recorded_response import RecordedResponseStream, capture_response_metadata
from tests.support.process_receipt import write_process_receipt


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
        if (attempt is None) == (judge is None):
            error = SharedControlError("A formal HTTP request requires one actual product RequestAttempt or SDK-owned judge call.")
            self.stop_control(error)
            raise error
        sdk_id = active_sdk_input.get()
        if sdk_id is None or not any(item["sdk_input_id"] == sdk_id for item in self.session.sdk_inputs):
            error = SharedControlError("A formal HTTP attempt lacks its complete observed SDK input; no network send occurred.")
            self.stop_control(error)
            raise error
        if judge is not None and judge.execution_id != self.gate.bound_execution_id:
            error = SharedControlError("Judge SDK call is outside its frozen execution identity.")
            self.stop_control(error)
            raise error
        identity = judge.request_identity if judge is not None else {name:getattr(attempt,name) for name in ("number","actor","purpose","delegation_id")}
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
            "ledger_number": number, "sdk_input_id": active_sdk_input.get(),
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
                    sdk = next(row for row in self.session.sdk_inputs if row["sdk_input_id"] == sdk_id)
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


async def main() -> None:
    authority_path = Path(os.environ["MYRK_ACCEPTANCE_AUTHORITY"])
    value, gate = verify_authority(authority_path)
    grant = value.grant
    gate.validate(case_id=value.case_id, protocol=value.protocol)
    write_process_receipt(Path(os.environ["MYRK_PROCESS_RECEIPT_DIRECTORY"]), __file__)
    role = os.environ["MYRK_ACCEPTANCE_ROLE"]
    directory = Path(os.environ["MYRK_DATA_DIRECTORY"])
    if value.case_id == "desktop.handoff":
        replies = [Reply(text="Formal handoff completed.")]
    else:
        from tests.support.agent_acceptance.formal_scenarios import replies as scenario_replies
        replies = scenario_replies(value.case_id, value.scenario_root or value.runtime, value.protocol, value.phase)
    session = OfflineSession(value.protocol, [], config=value.provider_config)
    terminal_mock = OfflineSession(value.protocol, replies, config=value.provider_config)
    from tests.support.agent_acceptance.compaction_continuity import FORMAL_CASES as CONTINUITY_CASES, ContinuityMock
    continuity = ContinuityMock(value.case_id,value.protocol,value.provider_config) if value.case_id in CONTINUITY_CASES and grant.transport == "mock" else None
    adoption = adoption_mock(value.case_id, value.protocol, config=value.provider_config) if grant.transport == "mock" and continuity is None else None
    directory.mkdir(parents=True, exist_ok=True)
    if value.case_id != "desktop.handoff":
        from tests.support.agent_acceptance.formal_scenarios import manifest

        approval = manifest(value.case_id, value.scenario_root or value.runtime, value.protocol)["approval"]
        if approval is not None:
            permissions = directory / "settings/permissions.json"
            permissions.parent.mkdir(parents=True, exist_ok=True)
            permissions.write_text(json.dumps(approval), encoding="utf-8")

    quality_terminal = None
    if value.case_id.startswith("quality.") and grant.transport == "mock":
        from tests.support.agent_acceptance.quality_runtime import terminal_mock as quality_mock
        quality_terminal = quality_mock(value.case_id, value.scenario_root or value.runtime, value.protocol, value.provider_config, phase=value.phase)

    pending_observations: list[dict[str, object]] = []
    stop_boundaries: list[dict[str, object]] = []
    original_emit = desktop_service.emit

    def observe_stop(event: str, **data: object) -> None:
        if event == "stopping":
            terminal = [{"attempt": row["attempt"], "sdk_input_id": row["sdk_input_id"]}
                        for row in session.exchanges if row.get("terminal_http_started") is True]
            stop_boundaries.append({"event": {"event": event, **data}, "terminal_attempts": terminal})
        original_emit(event, **data)

    def persist(*, finalizing: bool = False) -> None:
        attempts = {}
        task_id = ""
        for path in directory.rglob("*.json"):
            if path.parent != directory and not path.parent.name.endswith("-delegations"):
                continue
            record = json.loads(path.read_bytes())
            if "turns" in record:
                gate.observe_product_stop(path, case_id=value.case_id, protocol=value.protocol, finalizing=finalizing)
            for turn in record.get("turns", []):
                task_id = (turn.get("task") or {}).get("id", task_id)
                for attempt in [*turn.get("request_attempts", []), *(request for action in turn.get("actions", [])
                    if action.get("review") for request in action["review"].get("request_attempts", []))]:
                    attempts[(task_id, attempt["number"], attempt.get("actor"), attempt.get("purpose"))] = {"task_id": task_id, **attempt}
        exchange_path = session.save(value.runtime / role / "exchange.json", attempts=list(attempts.values()),
            run_id=value.run_id, case_id=value.case_id, task_id=task_id)
        if pending_observations or stop_boundaries:
            observed = json.loads(exchange_path.read_bytes())
            if pending_observations:
                observed["pending_observations"] = pending_observations
            if stop_boundaries:
                observed["stop_boundaries"] = stop_boundaries
            exchange_path.write_text(json.dumps(observed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (value.runtime / role / "transport.json").write_text(json.dumps(transport.observation, indent=2) + "\n", encoding="utf-8")

    async def respond(request: httpx2.Request) -> httpx2.Response:
        if role != "receiver":
            raise SharedControlError("The connection owner is not permitted to send model requests.")
        attempt = current_request_attempt()
        if quality_terminal is not None:
            quality_input = next(item for item in session.sdk_inputs if item["sdk_input_id"] == active_sdk_input.get())
            if attempt is None:
                raise SharedControlError("Quality terminal requires its actual native request purpose.")
            response = quality_terminal.respond(request, quality_input, attempt.purpose)
            if not isinstance(response, httpx2.Response):
                raise SharedControlError("Quality terminal returned an invalid terminal HTTP response.")
            from tests.support.agent_acceptance.quality_runtime import terminal_response_barrier as quality_response_barrier
            observed = await quality_response_barrier(value.case_id, value.scenario_root or value.runtime, len(session.exchanges) - 1, quality_input, response)
            if not isinstance(observed, httpx2.Response):
                raise SharedControlError("Quality terminal barrier returned an invalid HTTP response.")
            return observed
        if continuity is not None:
            continuity_input = next(item for item in session.sdk_inputs if item["sdk_input_id"] == active_sdk_input.get())
            if attempt is None: raise SharedControlError("Continuity fixture requires its actual request purpose.")
            return continuity.respond(request,continuity_input,attempt.purpose)
        if adoption is not None:
            captured = [item for item in session.sdk_inputs if item["sdk_input_id"] == active_sdk_input.get()]
            if attempt is None or len(captured) != 1:
                raise SharedControlError("The adoption-only Mock requires its actual SDK input and request purpose.")
            return adoption.respond(request, captured[0], purpose=attempt.purpose)
        if attempt is not None and attempt.purpose == "compaction" and value.case_id != "desktop.handoff":
            from tests.support.agent_acceptance.formal_scenarios import summary_reply
            terminal_mock.replies.insert(len(terminal_mock.exchanges), summary_reply(value.case_id))
        if value.case_id == "formal.recovery-family" and value.phase == "work" and len(terminal_mock.exchanges) == 1:
            assert value.scenario_root is not None
            (value.scenario_root / "family-child-arrived.flag").write_text(str(os.getpid()), encoding="utf-8")
            async with asyncio.timeout(30):
                while not (value.scenario_root / "family-child-release.flag").exists():
                    await asyncio.sleep(.025)
        response = terminal_mock.respond(request)
        from tests.support.agent_acceptance.independent_review import FORMAL_CASES, terminal_response_barrier
        if value.case_id in FORMAL_CASES:
            review_input = next(item for item in session.sdk_inputs if item['sdk_input_id'] == active_sdk_input.get())
            await terminal_response_barrier(value.case_id, value.scenario_root or value.runtime, review_input)
        from tests.support.agent_acceptance import queue_parallel_stop
        if value.case_id in queue_parallel_stop.SCENARIOS:
            queue_input = next(item for item in session.sdk_inputs if item["sdk_input_id"] == active_sdk_input.get())
            response = await queue_parallel_stop.terminal_response_barrier(value.case_id, value.scenario_root or value.runtime, len(terminal_mock.exchanges) - 1, queue_input, response)
        if value.case_id.startswith("quality."):
            from tests.support.agent_acceptance.quality_runtime import terminal_response_barrier as quality_response_barrier
            quality_input = next(item for item in session.sdk_inputs if item["sdk_input_id"] == active_sdk_input.get())
            observed = await quality_response_barrier(value.case_id, value.scenario_root or value.runtime, len(session.exchanges) - 1, quality_input, response)
            if not isinstance(observed, httpx2.Response):
                raise SharedControlError("Quality terminal barrier returned an invalid HTTP response.")
            return observed
        return response

    def local_failure() -> bool:
        if value.phase != "work" or role != "receiver":
            return False
        if value.case_id in {"formal.recovery-family", "quality.qg5.formal.8"}:
            return any(work["status"] == "completed" and turn.get("task", {}).get("supplements")
                for path in directory.glob("*.json") for turn in json.loads(path.read_bytes()).get("turns", [])
                for work in turn.get("delegations", []))
        if value.case_id != "formal.restart":
            return False
        return any(action["kind"] == "check" and action["status"] == "completed"
            for path in directory.glob("*.json") for turn in json.loads(path.read_bytes()).get("turns", [])
            for action in turn.get("actions", []))

    transport = _AuthorizedTransport(gate, value.case_id, session, role,
        httpx2.MockTransport(respond) if grant.transport == "mock" else httpx2.AsyncHTTPTransport(), persist, local_failure, product_records_root=directory, material_root=value.scenario_root,
        hold_stop_response=value.case_id == "quality.qg5.formal.9" and value.phase == "work",
        fault_origin="planned local pre-send failure after completed child" if value.case_id in {"formal.recovery-family", "quality.qg5.formal.8"} else "planned local pre-send failure after the real completed check")

    def local_save_boundary(event: str, args: tuple[object, ...]) -> None:
        if value.case_id not in {"formal.recovery-unknown", "quality.qg5.formal.6"} or value.phase != "work" or role != "receiver" or event != "os.rename":
            return
        pending = Path(str(args[0]))
        if not str(pending).endswith(".json.tmp") or not pending.resolve().is_relative_to(directory.resolve()):
            return
        raw = pending.read_bytes()
        record = json.loads(raw)
        if not record.get("turns") or not any(action["kind"] == "edit" and action["status"] == "completed" for action in record["turns"][-1]["actions"]):
            return
        root = value.scenario_root
        assert root is not None
        reliable = Path(str(args[1])).read_bytes()
        (root / "reliable-original.json").write_bytes(reliable)
        (root / "pending-original.json").write_bytes(raw)
        target = root / "materials/notes.txt"
        if value.case_id == "quality.qg5.formal.6":
            from tests.support.agent_acceptance.quality_runtime import bound_task
            from tests.support.agent_acceptance.quality_recovery_resume import facts as recovery_facts
            _, visible, _ = bound_task(value.case_id, root)
            target = root / "materials" / recovery_facts(visible).target
        (root / "effect-at-boundary.txt").write_bytes(target.read_bytes())
        pending_observations.extend({"source_path": str(pending), "source_sha256": sha256(raw).hexdigest(),
            "task_id": turn["task"]["id"], "attempts": [{"task_id": turn["task"]["id"], **attempt} for attempt in turn["request_attempts"]], "committed": False,
            "source_kind": "uncommitted_pending_record"} for turn in record["turns"] if turn.get("task"))
        persist()
        (root / "save-boundary.json").write_text(json.dumps({"kind": "local_save_boundary_exit", "pid": os.getpid(),
            "exit_code": 71, "reliable": str(args[1]), "pending": str(pending),
            "reliable_sha256": sha256(reliable).hexdigest(), "pending_sha256": sha256(raw).hexdigest()}, indent=2), encoding="utf-8")
        os._exit(71)

    sys.addaudithook(local_save_boundary)

    class OwnedClient(httpx2.AsyncClient):
        async def aclose(self) -> None:
            pass

    client = OwnedClient(transport=transport, trust_env=False)
    @asynccontextmanager
    async def default_model(key: str) -> AsyncIterator[Model]:
        async with deepseek_model(key, http_client=client) as model:
            yield model

    observe_boundary = value.case_id == "quality.qg3.formal.9" and value.phase == "work" and role == "receiver"
    observer = patch.object(desktop_service, "emit", observe_stop) if observe_boundary else nullcontext()
    with observe_saves(gate.root, value.runtime / role / "save-diagnostics.json"), observer:
        try:
            if grant.transport == "mock":
                models.ALLOW_MODEL_REQUESTS = True
            persist()
            await desktop_service.serve(directory, default_model, http_client=client, model_observer=lambda model: SDKCapture(model, session.sdk_inputs))
        finally:
            try:
                persist(finalizing=True)
            finally:
                await httpx2.AsyncClient.aclose(client)


if __name__ == "__main__":
    asyncio.run(main())
