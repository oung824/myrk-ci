"""Freeze a closed execution bundle, then account for every authorized send."""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from myrk.project_paths import project_root
from pathlib import Path
from pydantic import TypeAdapter
from pydantic_ai import CancellationToken
from tests.support.agent_acceptance.core import AcceptancePlan, CaseRegistry, Protocol, SharedControlError
from tests.support.agent_acceptance.eligibility import preparation_eligibility
from tests.support.agent_acceptance.preparation_gate_candidates import _CandidateGate
from tests.support.agent_acceptance.preparation_gate_requests import _RequestGate
from tests.support.agent_acceptance.preparation_gate_stops import _StopGate
from tests.support.agent_acceptance.preparation_gate_validation import (
    EVALUATOR_ISOLATION_DIRECTORIES,
    _ValidationGate,
    normalize_evaluator_isolation_directories,
)
from tests.support.agent_acceptance.preparation_models import (
    CandidateFreezeReview,
    CaseBudget,
    CaseBudgetExceeded,
    FrozenPrice,
    LiveAuthorization,
    LiveExecutionRequest,
    PreparedBundle,
    ProductOwnerOriginal,
    SOURCE,
    _LiveLedger,
    _ReservedAttempt,
    bundle_fingerprint,
    digest,
    restore_product_original,
    validate_product_originals,
)
from tests.support.agent_acceptance.preparation_snapshot import (
    bound_files,
    bundle_material_root,
    candidate_plan_matches,
    read_prepared_archive,
    read_prepared_snapshot,
    verify_prepared,
)
from tests.support.agent_acceptance.preparation_transaction import ACTIVE_CONTROLS, ledger_mutation
from typing import Any


def prepare_live(plan: AcceptancePlan, *, root: Path, registry: CaseRegistry,
                 budget_proposals: dict[str, dict[str, int]] | None = None,
                 evaluator_isolation_directories: list[str] | tuple[str, ...] | None = None) -> PreparedBundle:
    return _prepare_bundle(plan, root=root, registry=registry, budget_proposals=budget_proposals,
        evaluator_isolation_directories=evaluator_isolation_directories)


def prepare_offline_qualification(plan: AcceptancePlan, *, registry: CaseRegistry) -> PreparedBundle:
    """Freeze a mock qualification under the current owned check when available."""
    from uuid import uuid4

    from myrk.check_plugin import managed_check_run

    project = project_root(SOURCE)
    managed = os.environ.get("MYRK_CHECK_RUN_DIRECTORY")
    run = managed_check_run(Path(managed) / "fixtures") if managed else None
    if run is not None and run.parent == project / ".scratch/check-runs/runtime-tmp":
        root = run / "offline-qualification" / uuid4().hex
        return _prepare_bundle(plan, root=root, registry=registry, qualification_parent=root.parent)
    return prepare_live(plan, root=project / ".scratch/acceptance-runs" / uuid4().hex, registry=registry)


def _prepare_case_executions(plan: AcceptancePlan, root: Path, registry: CaseRegistry,
                             evaluator_isolation_directories: tuple[str, ...] | None) -> dict[str, Any]:
    execution: dict[str, Any] = {}
    preparations: dict[Any, dict[str, Any]] = {}
    for case, _ in registry.select(plan):
        if case.prepare is not None and case.case_id not in execution:
            if case.prepare not in preparations:
                preparations[case.prepare] = case.prepare(root)
            configuration = preparations[case.prepare]
            if (evaluator_isolation_directories is not None and plan.quality_packet is not None
                    and case.case_id.startswith("quality.")):
                configuration = {**configuration, EVALUATOR_ISOLATION_DIRECTORIES: list(evaluator_isolation_directories)}
            execution[case.case_id] = configuration
    return execution


def _prepare_bundle(plan: AcceptancePlan, *, root: Path, registry: CaseRegistry,
                    budget_proposals: dict[str, dict[str, int]] | None = None,
                    qualification_parent: Path | None = None,
                    evaluator_isolation_directories: list[str] | tuple[str, ...] | None = None) -> PreparedBundle:
    plan = AcceptancePlan.model_validate(plan.model_dump())
    registry.select(plan)
    isolation = (None if evaluator_isolation_directories is None
        else normalize_evaluator_isolation_directories(evaluator_isolation_directories))
    if plan.executions and (plan.parent_budget is None or plan.parent_budget.accounting_root.resolve() != root.resolve()):
        raise SharedControlError("Frozen executions require their original parent accounting root; no new parent grant is created.")
    if plan.mode != "prepare-live":
        raise ValueError("Preparation requires an explicit prepare-live plan.")
    if any(part.is_symlink() or part.is_junction() for part in (root, *root.parents)):
        raise SharedControlError("Prepared root cannot use links.")
    root = root.resolve()
    project = project_root(SOURCE)
    if (not root.is_relative_to(project / ".scratch")
            or (root.is_relative_to(project / ".scratch/check-runs") and root.parent != qualification_parent)):
        raise SharedControlError("Prepared root must be persistent inside project .scratch, outside disposable check runs.")
    root.mkdir(parents=True, exist_ok=False)
    (root / "owner.json").write_text(json.dumps({"owner": "agent-acceptance-preparation",
        "task_id": os.environ.get("MYRK_TASK_ID"), "project_root": str(project), "source_root": str(SOURCE),
        "runtime_directory": str(root), "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "Frozen acceptance execution bundle; preparation grants no sending authority.",
        "delete_when": "The run is complete or superseded and necessary original evidence is archived and verified."},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    execution = _prepare_case_executions(plan, root, registry, isolation)
    files = bound_files(plan, registry)
    from tests.support.agent_acceptance.reporting import environment_facts
    environment = environment_facts()
    routes = preparation_eligibility()
    prices = tuple(FrozenPrice(protocol=item.protocol, role=item.role, input_per_million=item.price_input_per_million,
        output_per_million=item.price_output_per_million, output_limit=item.total_output_limit) for item in routes) + (
        FrozenPrice(protocol="openai-chat", role="product", input_per_million="0.30", output_per_million="1.20", output_limit=4096),)
    proposals = {name: CaseBudget.model_validate(value) for name, value in (budget_proposals or {}).items()}
    keys = [item.execution_id for item in plan.executions] if plan.executions else plan.cases
    if set(proposals) - set(keys):
        raise ValueError("Budget proposal refers to an unselected case.")
    for name in keys:
        limit = execution.get(name, {}).get("case_limits", {}).get(name, 30)
        proposals.setdefault(name, CaseBudget(request_limit=limit, cost_limit_microusd=limit * max(item.reservation_microusd for item in prices)))
    for item in plan.executions:
        proposals[item.execution_id] = CaseBudget(request_limit=item.request_cap, cost_limit_microusd=item.cost_cap_microusd)
    bundle = PreparedBundle(root=root, source_root=SOURCE, plan=plan, files=files, binding="", execution=execution, environment=environment,
        eligibility=routes, budget_proposals=proposals, prices=prices)
    bundle.binding = bundle_fingerprint(bundle)
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SOURCE / name).read_bytes())
    (root / "prepared.json").write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8")
    ledger = _LiveLedger(version=2, integrity="", binding=bundle.binding, authorization_binding=None, stopped=False, attempts=[])
    ledger.integrity = digest(ledger.model_dump(mode="json", exclude={"integrity", "candidate_codec"} if ledger.candidate_codec is None else {"integrity"}))
    (root / "ledger.json").write_text(ledger.model_dump_json(indent=2, exclude={"candidate_codec"} if ledger.candidate_codec is None else None) + "\n", encoding="utf-8")
    return bundle


@dataclass
class LiveGate(_CandidateGate, _RequestGate, _StopGate, _ValidationGate):
    root: Path
    registry: CaseRegistry
    authorization: LiveAuthorization | None = None
    bound_execution_id: str | None = None

    @property
    def material_root(self) -> Path:
        return bundle_material_root(self.prepared())

    def prepared(self) -> PreparedBundle:
        return verify_prepared(self.root, registry=self.registry,
            binding=self.authorization.binding if self.authorization is not None else None)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize read-check-save against the same parent ledger in all processes."""
        import msvcrt
        with (self.root / "ledger-owner.lock").open("a+b") as lock:
            if (self.root / "ledger-owner.lock").stat().st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as error:
                raise SharedControlError("Parent ledger transaction is unavailable; no terminal send allowed.") from error
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    @contextmanager
    def running(self, token: CancellationToken) -> Iterator[None]:
        controls = ACTIVE_CONTROLS.setdefault(self.root.resolve(), [])
        entry = (self.bound_execution_id, token)
        controls.append(entry)
        try:
            yield
        finally:
            controls.remove(entry)
            if not controls:
                ACTIVE_CONTROLS.pop(self.root.resolve(), None)

    def for_execution(self, execution_id: str) -> "LiveGate":
        return replace(self, bound_execution_id=execution_id)

    def stop_key(self, case_id: str, protocol: Protocol) -> str:
        return self.bound_execution_id or f"{case_id}:{protocol}"

    def recorded_outcome(self) -> Any:
        from tests.support.agent_acceptance.core import CaseOutcome
        ledger = self._read_ledger()
        saved = ledger.execution_results.get(self.bound_execution_id or "")
        if saved is None:
            return None
        return CaseOutcome(**{**saved, "evidence": {name: Path(path) for name, path in saved["evidence"].items()}})

    @ledger_mutation
    def finish_execution(self, outcome: Any) -> None:
        if self.bound_execution_id is None:
            return
        ledger = self._read_ledger()
        if self.bound_execution_id in ledger.execution_results:
            raise SharedControlError("Frozen execution result cannot be replaced.")
        ledger.execution_results[self.bound_execution_id] = {**vars(outcome), "evidence": {name:str(path) for name,path in outcome.evidence.items()}}
        self._save_ledger(ledger)

    @contextmanager
    def owner(self) -> Iterator[None]:
        """Hold one OS lock for the complete formal execution, across processes."""
        import msvcrt

        with (self.root / "execution-owner.lock").open("a+b") as lock:
            if (self.root / "execution-owner.lock").stat().st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise SharedControlError("Another formal execution owns this prepared run; no connection acquired.") from error
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def _read_ledger(self) -> _LiveLedger:
        try:
            original = TypeAdapter(dict[str, Any]).validate_json((self.root / "ledger.json").read_bytes())
            if original.get("integrity") != digest({key: value for key, value in original.items() if key != "integrity"}):
                raise SharedControlError("Live accounting integrity changed; no terminal HTTP sent.")
            return _LiveLedger.model_validate(original)
        except (OSError, ValueError) as error:
            raise SharedControlError(f"Live accounting cannot be read: {error}") from error

    def _save_ledger(self, ledger: _LiveLedger) -> None:
        ledger.integrity = digest(ledger.model_dump(mode="json", exclude={"integrity", "candidate_codec"} if ledger.candidate_codec is None else {"integrity"}))
        path = self.root / "ledger.json"
        pending = path.with_suffix(".pending")
        try:
            with pending.open("x", encoding="utf-8") as output:
                output.write(ledger.model_dump_json(indent=2, exclude={"candidate_codec"} if ledger.candidate_codec is None else None) + "\n")
                output.flush()
                os.fsync(output.fileno())
            pending.replace(path)
        except OSError as error:
            raise SharedControlError(f"Live accounting cannot be saved: {error}") from error

    def execution_id(self, case_id: str, protocol: Protocol) -> str:
        return self.bound_execution_id or f"{self.root.name}:{case_id}:{protocol}:1"

    @ledger_mutation
    def mark_not_sent(self, number: int, *, fault_origin: str) -> None:
        ledger = self._read_ledger()
        attempt = ledger.attempts[number - 1]
        if attempt.state != "reserved":
            raise SharedControlError("Cannot release an attempt after terminal HTTP started.")
        attempt.state, attempt.occupied_microusd, attempt.fault_origin = "not_sent", 0, fault_origin
        self._save_ledger(ledger)

    @ledger_mutation
    def mark_response_complete(self, number: int, record: dict[str, Any]) -> None:
        from tests.support.agent_acceptance.recorded_response import response_evidence
        ledger = self._read_ledger()
        attempt = ledger.attempts[number - 1]
        attempt.state = "response_complete"
        view = response_evidence(record)
        if view["status"] == "decoded":
            usages = [event.get("usageMetadata" if attempt.protocol == "google" else "usage") for event in view["events"]]
            usages = [item for item in usages if isinstance(item, dict)]
            if usages:
                usage = usages[-1]
                input_count = usage.get("promptTokenCount" if attempt.protocol == "google" else "prompt_tokens")
                output_count = usage.get("candidatesTokenCount" if attempt.protocol == "google" else "completion_tokens")
                if attempt.protocol == "google":
                    thinking = usage.get("thoughtsTokenCount")
                    output_count = output_count + thinking if type(output_count) is int and type(thinking) is int else None
                if type(input_count) is int and type(output_count) is int and input_count >= 0 and output_count >= 0:
                    bundle = self.prepared()
                    price = next(item for item in bundle.prices if item.protocol == attempt.protocol and item.role == attempt.role)
                    attempt.usage_estimate_microusd = price.microusd(input_count, output_count)
        self._save_ledger(ledger)

    @ledger_mutation
    def mark_http_started(self, number: int) -> None:
        ledger = self._read_ledger()
        validate_product_originals(ledger.product_request_owners)
        attempt = ledger.attempts[number - 1]
        key = attempt.execution_id if self.bound_execution_id is not None else f"{attempt.case_id}:{attempt.protocol}"
        owner = ledger.product_request_owners.get(str(number))
        task_stopped = bool(owner and f"{owner['execution_id']}:{owner['task_id']}" in ledger.product_task_stops)
        if ledger.stopped or key in ledger.case_stops or task_stopped:
            raise CaseBudgetExceeded("Durable execution or actual product Task stop accepted before terminal HTTP start.")
        attempt.state = "http_started"
        self._save_ledger(ledger)
