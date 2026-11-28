"""Persist product stops and cancel their associated requests."""

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tests.support.agent_acceptance.core import Protocol, SharedControlError
from tests.support.agent_acceptance.preparation_models import CaseBudgetExceeded, digest
from tests.support.agent_acceptance.preparation_transaction import ACTIVE_CONTROLS, ledger_mutation
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tests.support.agent_acceptance.core import CaseRegistry
    from tests.support.agent_acceptance.preparation_models import LiveAuthorization, PreparedBundle, _LiveLedger
    from tests.support.agent_acceptance.preparation_transaction import _LedgerTransactionHost
    from typing import Protocol as TypingProtocol

    class _StopGateHost(_LedgerTransactionHost, TypingProtocol):
        root: Path
        registry: CaseRegistry
        authorization: LiveAuthorization | None
        bound_execution_id: str | None

        def validate(self, *, case_id: str, protocol: Protocol) -> tuple[PreparedBundle, LiveAuthorization, _LiveLedger]: ...
        def _validate(self, *, case_id: str, protocol: Protocol,
                      allow_stopped: bool) -> tuple[PreparedBundle, LiveAuthorization, _LiveLedger]: ...
        def execution_id(self, case_id: str, protocol: Protocol) -> str: ...
        def stop_key(self, case_id: str, protocol: Protocol) -> str: ...
        def _read_ledger(self) -> _LiveLedger: ...
        def _save_ledger(self, ledger: _LiveLedger) -> None: ...


class _StopGate:
    @ledger_mutation
    def request_stop(self: "_StopGateHost", *, scope: Literal["execution", "protocol", "shared"], reason: str,
                     source: Path, case_id: str, protocol: Protocol, expected_block: bool = False) -> None:
        bundle, grant, ledger = self.validate(case_id=case_id, protocol=protocol)
        if not source.is_file() or not reason.strip():
            raise SharedControlError("Stop requires the actual preserved observation and a reason.")
        event = {"scope":scope,"reason":reason,"source":str(source),"source_sha256":sha256(source.read_bytes()).hexdigest(),
            "expected_block":expected_block,"observed_at":datetime.now(timezone.utc).isoformat(),
            "partial_effects_check_required":not expected_block}
        ledger.scope_stops.append(event)
        affected = {item.execution_id for item in bundle.plan.executions if scope == "shared"
            or scope == "protocol" and item.protocol == protocol or scope == "execution" and item.execution_id == self.bound_execution_id}
        event["execution_ids"] = sorted(affected)
        event["case_id"],event["protocol"] = case_id,protocol
        if not expected_block:
            if bundle.plan.executions:
                for identity in affected:
                    ledger.case_stops.setdefault(identity,{"reason":reason,"stop_scope":scope,
                        "unfinished":"Remaining work was not authorized to send."})
            else:
                ledger.case_stops[self.stop_key(case_id, protocol)] = {"reason":reason,"stop_scope":scope}
            if scope == "shared":
                ledger.stopped, ledger.stop_reason = True, reason
            event["inflight_requests"] = [item.number for item in ledger.attempts if item.state in {"reserved","http_started"}
                and (not bundle.plan.executions or item.execution_id in affected)]
        if ledger.authorization is None:
            ledger.authorization, ledger.authorization_binding = grant, digest(grant.model_dump(mode="json"))
        self._save_ledger(ledger)
        if not expected_block:
            for active_identity, token in ACTIVE_CONTROLS.get(self.root.resolve(), []):
                if scope == "shared" or active_identity in affected or not bundle.plan.executions:
                    token.cancel()

    def accepted_stop(self: "_StopGateHost", *, case_id: str, protocol: Protocol, request_number: int | None = None) -> bool:
        ledger=self._read_ledger()
        owner = ledger.product_request_owners.get(str(request_number)) if request_number is not None else None
        task_stopped = bool(owner and f"{owner['execution_id']}:{owner['task_id']}" in ledger.product_task_stops)
        return task_stopped or ledger.stopped or any(not event["expected_block"] and
            (self.bound_execution_id in event.get("execution_ids",[]) if self.bound_execution_id is not None else
                event["scope"] == "shared" or event["protocol"] == protocol and
                (event["scope"] == "protocol" or event["case_id"] == case_id)) for event in ledger.scope_stops)

    @ledger_mutation
    def observe_product_stop(self: "_StopGateHost", record_path: Path, *, case_id: str, protocol: Protocol,
                             finalizing: bool = False) -> None:
        """Record a saved main-turn cancellation. Finalization leaves an already stopped ledger intact."""
        from myrk.conversation import ConversationRecord

        try:
            original = record_path.read_bytes()
            record = ConversationRecord.model_validate_json(original)
        except (OSError, ValueError) as error:
            raise SharedControlError(f"Product stop original cannot be read: {record_path}: {error}") from error
        cancelled = [turn for turn in record.turns if turn.request_owner_id is None and turn.status == "cancelled"]
        if not cancelled:
            return
        try:
            _, _, ledger = self._validate(case_id=case_id, protocol=protocol, allow_stopped=finalizing)
        except CaseBudgetExceeded:
            ledger = self._read_ledger()
        if finalizing and ledger.stopped:
            return
        bound = [item for item in ledger.attempts if item.case_id == case_id and item.protocol == protocol
            and item.execution_id == self.execution_id(case_id, protocol)]
        matches = [turn for turn in cancelled if any(item.product_number == request.number
            and item.product_step_id == request.step_id
            and (item.actor, item.purpose, item.delegation_id) == (request.actor, request.purpose, request.delegation_id)
            and request.observation is not None and request.observation.wire is not None
            and item.request_sha256 == request.observation.wire.body_sha256
            for item in bound for request in turn.request_attempts)]
        if not matches and not (record.turns and record.turns[-1] in cancelled
                and not record.turns[-1].request_attempts):
            return
        assert self.authorization is not None
        if ledger.authorization is None:
            ledger.authorization = self.authorization
            ledger.authorization_binding = digest(self.authorization.model_dump(mode="json"))
        if self.registry.cases[case_id].product_stop_scope == "task":
            task_ids = {turn.task.id for turn in matches if turn.task is not None}
            if not task_ids and record.turns and record.turns[-1] in cancelled and record.turns[-1].task is not None:
                task_ids.add(record.turns[-1].task.id)
            for task_id in task_ids:
                ledger.product_task_stops[f"{self.execution_id(case_id, protocol)}:{task_id}"] = {"reason": "product saved cancelled main turn; product Task stopped",
                    "source": str(record_path), "source_sha256": sha256(original).hexdigest()}
            for item in bound:
                owner = ledger.product_request_owners.get(str(item.number))
                if owner is not None and owner["task_id"] in task_ids and item.state in {"http_started", "unknown"}:
                    item.state, item.fault_origin = "unknown", "product saved cancelled Task before complete terminal response"
                    item.usage_estimate_microusd = None
            self._save_ledger(ledger)
            return
        ledger.case_stops[self.stop_key(case_id, protocol)] = {"reason": "product saved cancelled turn; execution stopped",
            "unfinished": "Remaining work was not authorized to send.",
            "source": str(record_path), "source_sha256": sha256(original).hexdigest()}
        for item in bound:
            if item.state in {"http_started", "unknown"}:
                item.state = "unknown"
                item.fault_origin = "product saved cancelled turn before complete terminal response"
                item.usage_estimate_microusd = None
        self._save_ledger(ledger)

    @ledger_mutation
    def mark_unknown(self: "_StopGateHost", number: int, *, fault_origin: str, stop_case: bool = False) -> None:
        ledger = self._read_ledger()
        attempt = ledger.attempts[number - 1]
        attempt.state, attempt.fault_origin = "unknown", fault_origin
        if stop_case:
            owner = ledger.product_request_owners.get(str(number))
            if self.registry.cases[attempt.case_id].product_stop_scope == "task":
                if owner is None:
                    raise SharedControlError("Cancelled scoped request lacks its actual product Task owner.")
                ledger.product_task_stops[f"{owner['execution_id']}:{owner['task_id']}"] = {"reason": "request cancelled; product Task stopped", "fault_origin": fault_origin}
            else:
                ledger.case_stops[self.stop_key(attempt.case_id, attempt.protocol)] = {"reason": "request cancelled; execution stopped",
                    "unfinished": "Remaining work was not authorized to send."}
        self._save_ledger(ledger)

    @ledger_mutation
    def stop(self: "_StopGateHost", *, reason: str = "shared control failed") -> None:
        ledger = self._read_ledger()
        ledger.stopped = True
        ledger.stop_reason = reason
        self._save_ledger(ledger)
        for _, token in list(ACTIVE_CONTROLS.get(self.root.resolve(), [])):
            token.cancel()
