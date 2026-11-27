"""Typed request evidence shared by QG3 stop scoring and publication."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, TYPE_CHECKING

from tests.support.agent_acceptance.core import CaseContext, SharedControlError

if TYPE_CHECKING:
    from tests.support.agent_acceptance.preparation import _LiveLedger, _ReservedAttempt


IDENTITY_FIELDS = ("number", "actor", "purpose", "delegation_id")
TERMINAL_STATES = frozenset({"http_started", "response_complete", "unknown"})


def stop_reservations(context: CaseContext, ledger: _LiveLedger) -> dict[int, _ReservedAttempt]:
    assert context.live_gate is not None
    scope = (context.case_id, context.protocol, context.live_gate.execution_id(context.case_id, context.protocol))
    matched = [item for item in ledger.attempts if (item.case_id, item.protocol, item.execution_id) == scope]
    by_number = {item.number: item for item in matched}
    if len(by_number) != len(matched):
        raise SharedControlError("Gated stop ledger repeats a reservation number.")
    return by_number


def nonblank(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def positive_number(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def request_identity(value: object) -> dict[str, str | int | None]:
    result: dict[str, str | int | None] = {}
    if not isinstance(value, dict):
        return result
    for field in (*IDENTITY_FIELDS, "judge_call_id"):
        if field in value:
            item = positive_number(value[field]) if field == "number" else nonblank(value[field])
            if item is not None or (field in {"delegation_id", "judge_call_id"} and value[field] is None):
                result[field] = item
    return result


def reservation_identity_conflicts(identity: dict[str, str | int | None], attempt: _ReservedAttempt) -> bool:
    expected: dict[str, str | int | None] = {"actor": attempt.actor, "purpose": attempt.purpose,
        "delegation_id": attempt.delegation_id, "judge_call_id": attempt.judge_call_id}
    if attempt.product_number is not None:
        expected["number"] = attempt.product_number
    return attempt.role != "product" or any(expected[key] != value for key, value in identity.items() if key in expected)


@dataclass(frozen=True)
class StopSdkInput:
    sdk_id: str | None
    identity: dict[str, str | int | None]

    @classmethod
    def from_original(cls, original: dict[str, Any]) -> StopSdkInput:
        return cls(nonblank(original.get("sdk_input_id")), request_identity(original.get("request_identity")))


@dataclass(frozen=True)
class StopRequestEvidence:
    original: dict[str, Any]
    ordinal: int | None
    sdk_id: str | None
    identity: dict[str, str | int | None]
    step_id: str | None
    ledger_number: int | None
    body_sha256: str | None
    terminal_started: bool | None
    not_sent: bool
    malformed_not_sent: bool

    @classmethod
    def from_original(cls, original: dict[str, Any]) -> StopRequestEvidence:
        body = nonblank(original.get("request_utf8"))
        try:
            fingerprint = sha256(body.encode()).hexdigest() if body is not None else None
        except UnicodeEncodeError:
            fingerprint = None
        terminal_started = original.get("terminal_http_started")
        not_sent = original.get("not_sent")
        return cls(original, positive_number(original.get("attempt")), nonblank(original.get("sdk_input_id")),
            request_identity(original.get("request_identity")), nonblank(original.get("product_step_id")),
            positive_number(original.get("ledger_number")), fingerprint,
            terminal_started if isinstance(terminal_started, bool) else None, not_sent is True,
            "not_sent" in original and not isinstance(not_sent, bool))

    def conflicts_with(self, attempt: _ReservedAttempt, sdk_identity: dict[str, str | int | None] | None = None) -> bool:
        bindings = ((self.sdk_id, attempt.sdk_input_id), (self.body_sha256, attempt.request_sha256),
                    (self.step_id, attempt.product_step_id))
        return (
            reservation_identity_conflicts(self.identity, attempt)
            or any(value is not None and expected is not None and value != expected for value, expected in bindings)
            or sdk_identity is not None and reservation_identity_conflicts(sdk_identity, attempt)
            or self.not_sent and (self.terminal_started is True or attempt.state != "not_sent")
            or self.terminal_started is True and attempt.state not in TERMINAL_STATES
            or self.terminal_started is False and attempt.state in TERMINAL_STATES
            or self.original.get("not_sent") is False and attempt.state == "not_sent"
        )

    def has_matching_terminal_reservation(self, attempt: _ReservedAttempt) -> bool:
        return (
            self.terminal_started is True and not self.not_sent and not self.malformed_not_sent
            and attempt.state in TERMINAL_STATES and attempt.product_number is not None
            and self.sdk_id is not None and self.sdk_id == attempt.sdk_input_id
            and self.step_id is not None and self.step_id == attempt.product_step_id
            and self.body_sha256 is not None and self.body_sha256 == attempt.request_sha256
            and not self.conflicts_with(attempt)
        )


@dataclass(frozen=True)
class StopSavedRequest:
    identity: dict[str, str | int | None]
    step_id: str | None
    body_sha256: str | None
    task_id: str | None

    def conflict(self, identity: dict[str, str | int | None], step_id: str | None, body_sha256: str | None) -> str | None:
        if any(self.identity[key] != value for key, value in identity.items() if key in self.identity):
            return "ownership"
        if step_id is not None and self.step_id is not None and step_id != self.step_id:
            return "step"
        if body_sha256 is not None and self.body_sha256 is not None and body_sha256 != self.body_sha256:
            return "body"
        return None

    @classmethod
    def from_original(cls, original: dict[str, Any]) -> StopSavedRequest:
        observation = original.get("observation")
        wire = observation.get("wire") if isinstance(observation, dict) else None
        return cls(request_identity(original), nonblank(original.get("step_id")),
            nonblank(wire.get("body_sha256")) if isinstance(wire, dict) else None,
            nonblank(original.get("task_id")))


@dataclass(frozen=True)
class StopSnapshot:
    event: dict[str, Any]
    sdk_by_ordinal: dict[int, str]

    @property
    def last_ordinal(self) -> int:
        return next(reversed(self.sdk_by_ordinal))

    def conflicts_with(self, request: StopRequestEvidence, reserved_sdk_id: str | None = None) -> bool:
        sdk_id = request.sdk_id or reserved_sdk_id
        expected_sdk = self.sdk_by_ordinal.get(request.ordinal) if request.ordinal is not None else None
        sdk_was_at_stop = sdk_id is not None and sdk_id in self.sdk_by_ordinal.values()
        if expected_sdk is not None or sdk_was_at_stop:
            if request.not_sent or request.terminal_started is False:
                return True
            if request.ordinal is not None and sdk_id is not None:
                return expected_sdk != sdk_id
        return (request.terminal_started is True and request.ordinal is not None
                and request.ordinal <= self.last_ordinal and expected_sdk is None)

    @classmethod
    def from_original(cls, value: object) -> StopSnapshot | None:
        if not isinstance(value, list) or len(value) != 1 or not isinstance(snapshot := value[0], dict):
            return None
        event = snapshot.get("event")
        rows = snapshot.get("terminal_attempts")
        if not isinstance(event, dict) or not isinstance(rows, list) or not rows:
            return None
        sdk_by_ordinal: dict[int, str] = {}
        sdk_ids: set[str] = set()
        preceding = 0
        for row in rows:
            if not isinstance(row, dict) or row.keys() != {"attempt", "sdk_input_id"}:
                return None
            ordinal = positive_number(row["attempt"])
            sdk_id = nonblank(row["sdk_input_id"])
            if ordinal is None or ordinal <= preceding or sdk_id is None or sdk_id in sdk_ids:
                return None
            sdk_by_ordinal[ordinal] = sdk_id
            sdk_ids.add(sdk_id)
            preceding = ordinal
        return cls(event, sdk_by_ordinal)


@dataclass(frozen=True)
class StopExchange:
    task_id: str | None
    requests: list[StopRequestEvidence]
    sdk_inputs: list[StopSdkInput]
    missing_sections: tuple[str, ...]
    stop_snapshot: StopSnapshot | None
    reservations: dict[int, _ReservedAttempt] | None = None

    @classmethod
    def from_original(cls, value: dict[str, Any]) -> StopExchange:
        missing = []

        def originals(field: str) -> list[dict[str, Any]]:
            rows = value.get(field)
            if not isinstance(rows, list):
                missing.append(field)
                return []
            objects = [row for row in rows if isinstance(row, dict)]
            if len(objects) != len(rows):
                missing.append(field)
            return objects

        requests = [StopRequestEvidence.from_original(row) for row in originals("exchanges")]
        sdk_inputs = [StopSdkInput.from_original(row) for row in originals("sdk_inputs")]
        return cls(nonblank(value.get("task_id")), requests, sdk_inputs, tuple(missing),
                   StopSnapshot.from_original(value.get("stop_boundaries")))
