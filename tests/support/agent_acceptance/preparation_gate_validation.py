"""Check the frozen plan, parent budget, and current transport authorization."""

import json
import os
from base64 import b64encode
from datetime import datetime, timezone
from hashlib import sha256
from myrk.project_paths import project_root
from pathlib import Path
from tests.support.agent_acceptance.core import AcceptancePlan, Protocol, SharedControlError
from tests.support.agent_acceptance.preparation_models import (
    CaseBudgetExceeded,
    LiveAuthorization,
    PreparedBundle,
    SOURCE,
    _LiveLedger,
    _ReservedAttempt,
    digest,
    restore_product_original,
    validate_product_originals,
)
from tests.support.agent_acceptance.preparation_snapshot import read_prepared_snapshot
from tests.support.agent_acceptance.preparation_transaction import ledger_mutation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tests.support.agent_acceptance.core import CaseRegistry
    from tests.support.agent_acceptance.preparation import LiveGate
    from tests.support.agent_acceptance.preparation_transaction import _LedgerTransactionHost
    from typing import Protocol as TypingProtocol

    class _ValidationGateHost(_LedgerTransactionHost, TypingProtocol):
        root: Path
        registry: CaseRegistry
        authorization: LiveAuthorization | None
        bound_execution_id: str | None

        def prepared(self) -> PreparedBundle: ...
        def parent_binding_path(self, parent_id: str) -> Path: ...
        def _parent_binding(self, bundle: PreparedBundle, grant: LiveAuthorization, *, activate: bool, required: bool) -> None: ...
        def _validate(self, *, case_id: str, protocol: Protocol,
                      allow_stopped: bool) -> tuple[PreparedBundle, LiveAuthorization, _LiveLedger]: ...
        def execution_id(self, case_id: str, protocol: Protocol) -> str: ...
        def stop_key(self, case_id: str, protocol: Protocol) -> str: ...
        def for_execution(self, execution_id: str) -> LiveGate: ...
        def _read_ledger(self) -> _LiveLedger: ...
        def _save_ledger(self, ledger: _LiveLedger) -> None: ...


EVALUATOR_ISOLATION_DIRECTORIES = "evaluator_isolation_directories"


def evaluator_isolation_paths(directories: list[str] | tuple[str, ...]) -> tuple[Path, ...]:
    if not isinstance(directories, (list, tuple)):
        raise SharedControlError("Frozen quality evaluator isolation directories must be an explicit list.")
    project = project_root(SOURCE)
    paths = []
    for directory in directories:
        if (not isinstance(directory, str)
                or any(not part or part in {".", ".."} or part.endswith((".", " ")) for part in directory.split("/"))
                or any(ord(char) < 32 or char in '\\:*?"<>|' for char in directory)):
            raise SharedControlError("Frozen quality evaluator isolation directories must be canonical project-relative paths.")
        path = project / directory
        try:
            resolved = path.resolve()
        except OSError as error:
            raise SharedControlError("Frozen quality evaluator isolation directories cannot be resolved.") from error
        if (resolved != path or not resolved.is_relative_to(project)
                or path.is_symlink() or path.is_junction() or (path.exists() and not path.is_dir())):
            raise SharedControlError("Frozen quality evaluator isolation directories must stay inside the project without links.")
        paths.append(path)
    return tuple(paths)


def normalize_evaluator_isolation_directories(directories: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    evaluator_isolation_paths(directories)
    return tuple(sorted(set(directories)))


def frozen_evaluator_isolation_directories(bundle: PreparedBundle, case_id: str) -> tuple[str, ...] | None:
    execution = bundle.execution.get(case_id, {})
    if not isinstance(execution, dict):
        raise SharedControlError("Frozen quality execution configuration is invalid.")
    declaration = execution.get(EVALUATOR_ISOLATION_DIRECTORIES)
    return None if declaration is None else normalize_evaluator_isolation_directories(declaration)


class _ValidationGate:
    def validate_plan(self: "_ValidationGateHost", plan: AcceptancePlan) -> None:
        bundle = self.prepared()
        if (plan.executions or bundle.plan.executions) and plan.model_dump(exclude={"mode"}) != bundle.plan.model_dump(exclude={"mode"}):
            raise SharedControlError("Frozen plan changed: execution identities, order, dependencies, caps and material bindings are immutable.")

    def parent_binding_path(self, parent_id: str) -> Path:
        return project_root(SOURCE) / ".scratch/acceptance-parent-budgets" / (sha256(parent_id.encode()).hexdigest() + ".json")

    def _parent_binding(self: "_ValidationGateHost", bundle: PreparedBundle, grant: LiveAuthorization, *, activate: bool,
                        required: bool) -> None:
        parent = bundle.plan.parent_budget
        if parent is None:
            return
        path = self.parent_binding_path(parent.parent_id)
        if bundle.candidate_directory is not None:
            ledger = self._read_ledger()
            bundle = read_prepared_snapshot(self.root, binding=ledger.binding)
            if ledger.authorization is None:
                raise SharedControlError("Candidate lost its original parent authorization.")
            grant = ledger.authorization
        expected = {"version":1,"parent_id":parent.parent_id,"accounting_root":str(bundle.root.resolve()),
            "binding":bundle.binding,"authorization_binding":digest(grant.model_dump(mode="json")),
            "project_root":str(project_root(SOURCE)),"owner":"acceptance-parent-control",
            "purpose":"Bind this activated parent to its original accounting root; preparation grants no authority.",
            "delete_when":"Persistent control state; retain with the original grant and ledger until their authorized lifecycle ends."}
        if activate and not path.exists():
            path.parent.mkdir(parents=True,exist_ok=True)
            try:
                with path.open("x",encoding="utf-8") as output:
                    output.write(json.dumps(expected,ensure_ascii=False,indent=2)+"\n")
                    output.flush()
                    os.fsync(output.fileno())
            except FileExistsError:
                pass
            except OSError as error:
                raise SharedControlError(f"Parent accounting activation cannot be saved: {error}") from error
        if required or path.exists():
            try:
                original=json.loads(path.read_bytes())
            except (OSError,ValueError) as error:
                raise SharedControlError(f"Parent accounting activation is missing or damaged: {error}") from error
            if original != expected:
                raise SharedControlError("Parent accounting is already bound to its original root and authorization; no replacement budget is issued.")

    def validate(self: "_ValidationGateHost", *, case_id: str, protocol: Protocol) -> tuple[PreparedBundle, LiveAuthorization, _LiveLedger]:
        return self._validate(case_id=case_id, protocol=protocol, allow_stopped=False)

    def _validate(self: "_ValidationGateHost", *, case_id: str, protocol: Protocol, allow_stopped: bool) -> tuple[PreparedBundle, LiveAuthorization, _LiveLedger]:
        bundle = self.prepared()
        grant = self.authorization
        if grant is None or grant.binding != bundle.binding:
            raise SharedControlError("Missing or mismatched live authorization; no connection acquired.")
        allowed = {(case.case_id, item) for case, item in self.registry.select(bundle.plan)}
        executions = {item.execution_id: item for item in bundle.plan.executions}
        if (case_id, protocol) not in allowed:
            raise SharedControlError("Request is outside the authorized case/protocol binding.")
        if (bundle.plan.quality_packet is not None and case_id.startswith("quality.")
                and frozen_evaluator_isolation_directories(bundle, case_id) is None):
            raise SharedControlError("Frozen quality execution requires an explicit evaluator isolation directory list.")
        key = self.bound_execution_id if executions else case_id
        allowed_keys = set(executions) if executions else {name for name, _ in allowed}
        if executions and (key not in executions or (executions[key].case_id, executions[key].protocol) != (case_id, protocol)):
            raise SharedControlError("Request lacks its frozen independent execution identity.")
        if (set(grant.case_limits) - allowed_keys or grant.total_limit > sum(grant.case_limits.values())
                or (self.root / "ledger.pending").exists()):
            raise SharedControlError("Live accounting or granted limits differ from the frozen case plan.")
        if grant.transport == "real":
            routes = [item for item in bundle.eligibility if item.protocol == protocol and item.role == "product"]
            gaps = [gap.code for item in routes for gap in item.unresolved]
            raise SharedControlError("Paid route unqualified: " + ", ".join(gaps or ["unsupported_paid_route"]) +
                                     "; no connection acquired and no terminal HTTP sent.")
        assert key is not None
        proposal = bundle.budget_proposals[key]
        if (key not in grant.case_limits or grant.total_microusd is None or set(grant.case_microusd) != set(grant.case_limits)
                or grant.total_microusd > sum(grant.case_microusd.values())):
            raise SharedControlError("Missing explicit amount authorization; no connection acquired.")
        if (grant.case_limits.get(key, 0) > proposal.request_limit
                or grant.case_microusd.get(key, 0) > proposal.cost_limit_microusd):
            raise SharedControlError("Live authorization exceeds the frozen per-case send proposal.")
        parent = bundle.plan.parent_budget
        if executions and (parent is None or grant.parent_budget_id != parent.parent_id
                or grant.total_limit > parent.request_cap or grant.total_microusd > parent.total_microusd):
            raise SharedControlError("Parent budget identity or explicit grant exceeds the frozen parent.")
        ledger = self._read_ledger()
        attempts = ledger.attempts
        self._parent_binding(bundle,grant,activate=False,required=bool(attempts))
        grant_binding = digest(grant.model_dump(mode="json"))
        candidate = next((item for item in ledger.candidates if item["candidate_binding"] == bundle.binding), None)
        authorized_ids = {grant.authorization_id}
        if ledger.authorization is not None:
            authorized_ids.add(ledger.authorization.authorization_id)
        authorized_ids.update(item["grant"]["authorization_id"] for item in ledger.candidates)
        if ((ledger.stopped and not allow_stopped)
                or ledger.active_candidates.get(key, ledger.binding) != bundle.binding
                or (candidate is None and (ledger.binding != bundle.binding or ledger.authorization not in (None, grant)))
                or (candidate is not None and (candidate["grant"] != grant.model_dump(mode="json")
                    or ledger.active_candidates.get(key, ledger.binding) != bundle.binding))
                or ledger.authorization_binding != (digest(ledger.authorization.model_dump(mode="json")) if ledger.authorization is not None else None)
                or any(item.authorization_id not in authorized_ids or item.number != number
                       or (item.case_id, item.protocol) not in allowed for number, item in enumerate(attempts, 1))):
            raise SharedControlError("Live accounting is stopped or authorization changed.")
        validate_product_originals(ledger.product_request_owners)
        prices = {(item.protocol, item.role): item for item in bundle.prices}
        def attempt_key(item: _ReservedAttempt) -> str:
            return item.execution_id if executions else item.case_id
        if (len(attempts) > grant.total_limit or sum(item.occupied_microusd for item in attempts) > grant.total_microusd
                or any(sum(attempt_key(item) == name for item in attempts) > limit for name, limit in grant.case_limits.items())
                or any((item.execution_id not in executions or
                        (item.case_id, item.protocol, item.role, item.stage) != (executions[item.execution_id].case_id,
                            executions[item.execution_id].protocol, executions[item.execution_id].role, executions[item.execution_id].stage)) if executions
                        else item.execution_id != self.execution_id(item.case_id, item.protocol) for item in attempts)
                or any((item.protocol, item.role) not in prices
                    or item.reservation_microusd != prices[item.protocol, item.role].reservation_microusd
                    or item.price_version != prices[item.protocol, item.role].version
                    or item.occupied_microusd != (0 if item.state == "not_sent" else item.reservation_microusd)
                    or item.state == "not_sent" and not item.fault_origin for item in attempts)):
            raise SharedControlError("Live accounting amount, execution or price identity changed.")
        if parent is not None and ledger.phase_microusd:
            expected = dict(parent.phase_microusd)
            reserve = parent.reserve_microusd
            for transfer in ledger.transfers:
                expected[transfer["stage"]] += transfer["delta_microusd"]
                reserve -= transfer["delta_microusd"]
                basis = Path(transfer["basis"])
                if not basis.is_file() or sha256(basis.read_bytes()).hexdigest() != transfer["basis_sha256"]:
                    raise SharedControlError("Reserve accounting basis changed.")
            if ledger.phase_microusd != expected or ledger.reserve_microusd != reserve or reserve < 0:
                raise SharedControlError("Parent reserve accounting no longer conserves the frozen total.")
        if self.stop_key(case_id, protocol) in ledger.case_stops:
            raise CaseBudgetExceeded(ledger.case_stops[self.stop_key(case_id, protocol)]["reason"] + "; live case stopped; no connection acquired.")
        return bundle, grant, ledger

    @ledger_mutation
    def transfer_reserve(self: "_ValidationGateHost", *, stage: str, delta_microusd: int, basis: Path) -> None:
        bundle = self.prepared()
        if not bundle.plan.executions or bundle.plan.parent_budget is None:
            raise SharedControlError("Reserve transfers require the frozen parent budget.")
        saved = self._read_ledger()
        active = next((row for row in bundle.plan.executions
            if (self.bound_execution_id is None or row.execution_id == self.bound_execution_id)
            and saved.active_candidates.get(row.execution_id, saved.binding) == bundle.binding), None)
        if active is None:
            raise SharedControlError("Reserve transfer requires an execution active under this candidate.")
        _, grant, ledger = self.for_execution(active.execution_id).validate(case_id=active.case_id, protocol=active.protocol)
        parent = bundle.plan.parent_budget
        if not ledger.phase_microusd:
            ledger.phase_microusd = dict(parent.phase_microusd)
            ledger.reserve_microusd = parent.reserve_microusd
        if (stage not in ledger.phase_microusd or type(delta_microusd) is not int
                or not 0 < delta_microusd <= ledger.reserve_microusd):
            raise SharedControlError("Frozen reserve is insufficient or transfer is invalid.")
        if not basis.resolve().is_relative_to(self.root.resolve()) or not basis.is_file():
            raise SharedControlError("Reserve transfer basis must be a preserved original under the parent root.")
        ledger.phase_microusd[stage] += delta_microusd
        ledger.reserve_microusd -= delta_microusd
        ledger.transfers.append({"stage":stage,"delta_microusd":delta_microusd,"basis":str(basis),
            "basis_sha256":sha256(basis.read_bytes()).hexdigest(),"before_request":len(ledger.attempts)+1,
            "recorded_at":datetime.now(timezone.utc).isoformat()})
        if ledger.authorization is None:
            ledger.authorization, ledger.authorization_binding = grant, digest(grant.model_dump(mode="json"))
        self._save_ledger(ledger)

    def accounting_report(self: "_ValidationGateHost") -> dict[str, Any]:
        ledger = self._read_ledger()
        owners = {number: {**owner, 'sources': [
            {**{key: value for key, value in source.items() if key not in {'original_codec', 'original_compressed_base64'}},
                'original_base64': b64encode(restore_product_original(source)).decode('ascii')}
            for source in owner['sources']]} for number, owner in ledger.product_request_owners.items()}
        grant = ledger.authorization or self.authorization
        return {"version": ledger.version, "binding": ledger.binding, "stopped": ledger.stopped, "stop_reason": ledger.stop_reason,
            "parent_budget_id":grant.parent_budget_id if grant else None,
            "parent_binding_path":str(self.parent_binding_path(grant.parent_budget_id)) if grant is not None and grant.parent_budget_id is not None else None,
            "request_limit": grant.total_limit if grant else None, "cost_limit_microusd": grant.total_microusd if grant else None,
            "sdk_inputs": len({item.sdk_input_id for item in ledger.attempts if item.sdk_input_id}),
            "completed_responses": sum(item.state == "response_complete" for item in ledger.attempts),
            "usage_estimates": sum(item.usage_estimate_microusd is not None for item in ledger.attempts),
            "supplier_final_microusd": None,
            "reserved_requests": len(ledger.attempts), "not_sent": sum(item.state == "not_sent" for item in ledger.attempts),
            "actual_http": sum(item.state in {"http_started", "response_complete", "unknown"} for item in ledger.attempts),
            "occupied_microusd": sum(item.occupied_microusd for item in ledger.attempts),
            "unknown_fee_requests": sum(item.state != "not_sent" and item.supplier_final_microusd is None for item in ledger.attempts),
            "case_stops": ledger.case_stops, "product_request_owners": owners, "product_task_stops": ledger.product_task_stops, "phase_microusd": ledger.phase_microusd,
            "reserve_microusd": ledger.reserve_microusd, "reserve_transfers": ledger.transfers,
            "completed_executions": list(ledger.execution_results), "scope_stops":ledger.scope_stops,
            "candidates":ledger.candidates,"active_candidates":ledger.active_candidates,
            "role_usage": {role: sum(item.actor == role for item in ledger.attempts) for role in sorted({item.actor for item in ledger.attempts})},
            "attempts": [item.model_dump(mode="json") for item in ledger.attempts]}
