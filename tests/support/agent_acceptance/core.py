"""Typed plans and isolated case execution for agent acceptance checks."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from contextlib import nullcontext
from pydantic_ai import CancellationToken
import json
from hashlib import sha256
from pathlib import Path
import re
import traceback
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tests.support.agent_acceptance.frozen import FrozenExecution, FrozenParentBudget, source_material_path
from tests.support.agent_acceptance.quality_packet import QualityPacketRef

if TYPE_CHECKING:
    import httpx2
    from myrk.provider import ProviderConfig
    from tests.support.agent_acceptance.replay import AcceptanceSession, Reply, SessionFactory
    from tests.support.agent_acceptance.preparation import LiveGate

Protocol = Literal["deepseek", "openai-chat", "google"]
Mode = Literal["offline", "prepare-live", "live"]


class SharedControlError(RuntimeError):
    """Authorization, accounting or evidence cannot be trusted; stop the run."""


StopStatus = Literal["passed", "failed", "unknown"]


@dataclass(frozen=True)
class StopViolation:
    request_status: StopStatus
    action_status: StopStatus

    @classmethod
    def from_outcome(cls, outcome: "CaseOutcome") -> "StopViolation | None":
        if outcome.critical_contracts.get("K3") != "failed":
            return None
        request = outcome.facts.get("request_stop_status")
        action = outcome.facts.get("action_stop_status")
        if request != "failed" and action != "failed":
            return None
        return cls("failed" if request == "failed" else "unknown",
                   "failed" if action == "failed" else "unknown")


class IncompleteEvidenceError(SharedControlError):
    """A required original is unavailable; independently bound stop violations may survive."""

    def __init__(self, message: str, *, stop_violation: StopViolation | None = None) -> None:
        super().__init__(message)
        self.stop_violation = stop_violation


class AcceptancePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cases: list[str] = Field(default_factory=list)
    executions: tuple[FrozenExecution, ...] = ()
    parent_budget: FrozenParentBudget | None = None
    protocols: list[Protocol] = Field(default=["deepseek"], min_length=1)
    mode: Mode = "offline"
    historical_groups: tuple[Path, ...] = ()
    quality_packet: QualityPacketRef | None = None

    @field_validator("cases", "protocols")
    @classmethod
    def unique_names(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", item) for item in values):
            raise ValueError("Selections must contain unique, portable identifiers.")
        return values


    @model_validator(mode="after")
    def planned_selection(self) -> "AcceptancePlan":
        if bool(self.cases) == bool(self.executions):
            raise ValueError("Select legacy cases or explicit frozen executions, exclusively.")
        if self.executions:
            ids = [item.execution_id for item in self.executions]
            starts = [item.startpoint for item in self.executions]
            if len(ids) != len(set(ids)) or len(starts) != len(set(starts)):
                raise ValueError("Frozen execution IDs and isolated startpoints must be unique.")
            seen: set[str] = set()
            for number, item in enumerate(self.executions, 1):
                if item.order != number or not set(item.dependencies) <= seen:
                    raise ValueError("Frozen order and preceding dependency execution bindings are inconsistent.")
                seen.add(item.execution_id)
            shared = [(item, alias) for item in self.executions for alias in item.shared_with]
            if (len(shared) > 12 or len({alias.scenario_id for _,alias in shared}) != len(shared)
                    or len({item.material_family_id for item,_ in shared}) != len(shared)
                    or any(len(item.shared_with) > 1 or item.stage != "screening" or item.role != "product"
                        or item.material_family_id != alias.material_family_id or item.material_sha256 != alias.material_sha256
                        or item.request_cap > alias.request_cap for item,alias in shared)):
                raise ValueError("Shared execution coverage requires one identical, untuned frozen sample and the stricter cap; at most twelve.")
            groups: dict[tuple[str, str, str], list[FrozenExecution]] = {}
            for item in self.executions:
                groups.setdefault((item.scenario_id, item.protocol, item.stage), []).append(item)
            for rows in groups.values():
                if any(item.risk_branch for item in rows):
                    if (len(rows) != 3 or len({item.case_id for item in rows}) != 1 or len({item.risk_branch for item in rows}) != 1
                            or len({item.material_family_id for item in rows}) != 1
                            or [item.material_variant for item in rows] != ["baseline", "baseline", "variant"]
                            or rows[0].material_sha256 != rows[1].material_sha256
                            or rows[0].material_sha256 == rows[2].material_sha256
                            or any(item.equivalence_path is None or item.equivalence_sha256 is None for item in rows)
                            or len({item.equivalence_sha256 for item in rows}) != 1):
                        raise ValueError("Risk screening freezes two baseline executions and one actual equivalent variant on one risk branch.")
                elif len(rows) != 1:
                    raise ValueError("Ordinary screening runs once; repeated samples require the frozen risk arrangement.")
        return self


@dataclass(frozen=True)
class CaseContext:
    root: Path
    protocol: Protocol
    run_id: str
    case_id: str
    actor: str = "main"
    purpose: str = "work"
    session_factory: "SessionFactory | None" = None
    mode: Mode = "offline"
    live_gate: "LiveGate | None" = None
    execution: FrozenExecution | None = None
    cancellation_token: CancellationToken = field(default_factory=CancellationToken)
    dependency_evidence: dict[str, dict[str, Path]] = field(default_factory=dict)

    @property
    def execution_id(self) -> str:
        if self.execution is not None:
            return self.execution.execution_id
        if self.live_gate is not None:
            return self.live_gate.execution_id(self.case_id, self.protocol)
        return f"{self.run_id}:{self.case_id}:{self.protocol}:1"

    @property
    def material_input(self) -> str:
        if self.execution is None:
            raise SharedControlError("This legacy case has no explicit frozen material input.")
        path = source_material_path(self.execution.material_path)
        if self.live_gate is not None:
            path = self.live_gate.material_root / self.execution.material_path
        return path.read_text(encoding="utf-8")

    def session(self, replies: list["Reply"], *, config: "ProviderConfig | None" = None,
                transport: "httpx2.MockTransport | None" = None) -> "AcceptanceSession":
        from tests.support.agent_acceptance.replay import OfflineSession

        if self.execution is not None and self.execution.role != "product":
            raise SharedControlError("A frozen judge call cannot impersonate a product RequestAttempt.")
        if self.mode == "live":
            raise SharedControlError("Live sessions run in the authorized formal service; local model fallback is forbidden.")
        if self.live_gate is not None:
            return OfflineSession(self.protocol, replies, config=config, gate=self.live_gate, case_id=self.case_id,
                product_record=self.root / "conversation.json", terminal_transport=transport)
        factory = self.session_factory or OfflineSession
        return factory(self.protocol, replies, config=config)


    def judge_session(self, replies: list["Reply"], *, purpose: str) -> "AcceptanceSession":
        from tests.support.agent_acceptance.replay import OfflineSession
        from tests.support.agent_acceptance.preparation import verify_prepared
        if (self.live_gate is None or self.execution is None or self.execution.role != "judge"
                or self.execution.judge_purpose != purpose):
            raise SharedControlError("Judge identity and purpose require the same frozen parent authorization.")
        bundle = verify_prepared(self.live_gate.root, registry=self.live_gate.registry)
        route = next(item for item in bundle.eligibility if item.protocol == self.protocol and item.role == "judge")
        return OfflineSession(self.protocol, replies, config=route.configuration, gate=self.live_gate,
            case_id=self.case_id, judge_purpose=purpose, execution_id=self.execution_id)


@dataclass
class CaseOutcome:
    product_status: Literal["passed", "failed", "inconclusive", "not_executed"]
    facts: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Path] = field(default_factory=dict)
    tool_status: Literal["passed", "failed"] = "passed"
    semantic_status: Literal["not_measured", "passed", "failed"] = "not_measured"
    case_id: str = ""
    protocol: str = ""
    expectation_status: Literal["met", "unmet", "review_required", "not_executed"] = "unmet"
    expectation: str = "success"
    execution_id: str = ""
    screening: dict[str, Any] = field(default_factory=dict)
    case_status: Literal["passed", "failed", "pending_review", "not_executed"] = "pending_review"
    target_status: Literal["passed", "failed", "pending_review", "not_executed"] = "pending_review"
    evidence_status: Literal["complete", "incomplete", "unknown"] = "unknown"
    deterministic_status: Literal["passed", "failed", "not_measured"] = "not_measured"
    content_status: Literal["pending_review", "passed", "failed", "not_applicable"] = "pending_review"
    critical_contracts: dict[str, str] = field(default_factory=lambda: {name: "unknown" for name in ("K1", "K2", "K3", "K4", "K5")})
    user_acceptance: Literal["pending", "accepted", "rejected"] = "pending"
    execution_status: Literal["passed", "failed", "unknown", "invalid", "not_executed"] = "unknown"


@dataclass(frozen=True)
class AcceptanceCase:
    case_id: str
    capability: str
    protocols: tuple[Protocol, ...]
    run: Callable[[CaseContext], Awaitable[CaseOutcome]]
    bindings: tuple[Path, ...] = ()
    prepare: Callable[[Path], dict[str, Any]] | None = None
    formal_live: bool = False
    frozen_material_input: bool = False
    product_stop_scope: Literal["execution", "task"] = "execution"
    group: str = ""
    observation_layer: str = "conversation-sdk"
    expectation: str = "success"
    owner_ticket: str = ""
    dependencies: tuple[str, ...] = ()
    screening: dict[str, dict[str, Any]] = field(default_factory=dict)
    applicability: str = "Model behavior uses the listed native protocols; fixed replies do not measure semantics."
    output_standards: dict[str, dict[str, Any]] | None = None


class CaseRegistry:
    def __init__(self) -> None:
        self.cases: dict[str, AcceptanceCase] = {}
        self.quality_packet: QualityPacketRef | None = None

    def register(self, case: AcceptanceCase) -> None:
        if case.case_id in self.cases or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", case.case_id):
            raise ValueError(f"Invalid or duplicate case: {case.case_id}")
        from tests.support.agent_acceptance.catalogue import describe
        self.cases[case.case_id] = describe(case)

    def capabilities(self) -> dict[str, list[str]]:
        return {capability: [case.case_id for case in self.cases.values() if case.capability == capability]
                for capability in sorted({case.capability for case in self.cases.values()})}

    def catalogue(self) -> list[dict[str, Any]]:
        import inspect

        return [{"case_id": case.case_id, "capability": case.capability, "group": case.group,
            "protocols": list(case.protocols), "observation_layer": case.observation_layer,
            "expectation": case.expectation, "owner_ticket": case.owner_ticket,
            "bindings": [str(Path(inspect.getfile(case.run))), *map(str, case.bindings)],
            "applicability": case.applicability, "formal_live": case.formal_live, "product_stop_scope": case.product_stop_scope,
            "dependencies": list(case.dependencies), "screening": case.screening, "output_standards": case.output_standards, "sending_authorized": False} for case in self.cases.values()]

    def select(self, plan: AcceptancePlan) -> list[tuple[AcceptanceCase, Protocol]]:
        selected = []
        if plan.executions:
            by_id = {item.execution_id: item for item in plan.executions}
            resolved_bindings: dict[str, set[Path]] = {}
            for item in plan.executions:
                case = self.cases.get(item.case_id)
                if case is None or item.protocol not in case.protocols or not case.frozen_material_input:
                    raise ValueError(f"Missing implemented frozen material adapter: {item.case_id}/{item.protocol}")
                if case.case_id not in resolved_bindings:
                    resolved_bindings[case.case_id] = {path.resolve() for path in case.bindings}
                bindings = resolved_bindings[case.case_id]
                material = source_material_path(item.material_path)
                if (material.resolve() not in bindings
                        or not material.is_file()
                        or sha256(material.read_bytes()).hexdigest() != item.material_sha256):
                    raise ValueError(f"Missing or changed frozen material: {item.execution_id}")
                standards: list[tuple[Path,str | None]] = [(alias.criteria_path,alias.criteria_sha256) for alias in item.shared_with]
                if item.equivalence_path is not None:
                    standards.append((item.equivalence_path,item.equivalence_sha256))
                for relative_path, expected in standards:
                    path = source_material_path(relative_path)
                    if path.resolve() not in bindings or not path.is_file() or sha256(path.read_bytes()).hexdigest() != expected:
                        raise ValueError(f"Missing actual equivalence or shared criteria binding: {item.execution_id}")
                screening = case.screening.get(item.protocol)
                if item.stage == "screening" and screening is not None and (item.scenario_id != screening["scenario_id"] or item.request_cap > screening["request_cap"]):
                    raise ValueError(f"Frozen scenario or cap differs from its registered branch: {item.execution_id}")
                actual = {by_id[name].case_id for name in item.dependencies}
                if actual != set(case.dependencies):
                    raise ValueError(f"Frozen dependencies differ from actual case dependencies: {item.execution_id}")
                selected.append((case, item.protocol))
            return selected
        for name in plan.cases:
            if name not in self.cases:
                raise ValueError(f"Required acceptance case is missing: {name}")
            case = self.cases[name]
            for protocol in plan.protocols:
                if protocol not in case.protocols:
                    raise ValueError(f"Required protocol is unavailable: {name}/{protocol}")
                selected.append((case, protocol))
        return selected


@dataclass
class RunResult:
    run_id: str
    cases: list[CaseOutcome]
    result_path: Path
    version: dict[str, Any] = field(default_factory=dict)
    catalogue: list[dict[str, Any]] = field(default_factory=list)
    planned_executions: tuple[FrozenExecution, ...] = ()
    historical_groups: tuple[Path, ...] = ()

    def save(self, mode: Mode) -> None:
        from tests.support.agent_acceptance.history import read_history

        history = read_history()
        from tests.support.agent_acceptance.historical_reuse import historical_group_reports
        seen_pairs: set[tuple[str, str]] = set()
        seen_ids: set[str] = set()
        for item in self.cases:
            if not item.execution_id:
                item.execution_id = f"{self.run_id}:{item.case_id}:{item.protocol}:1"
            if self.planned_executions and item.execution_id not in {row.execution_id for row in self.planned_executions}:
                raise SharedControlError("An outcome is outside the original frozen execution denominator.")
            item.execution_status = ("invalid" if item.tool_status == "failed" or item.evidence_status == "incomplete"
                else "not_executed" if item.product_status == "not_executed" else "unknown" if item.product_status == "inconclusive"
                else item.product_status)
            pair = (item.case_id, item.protocol)
            if (pair in seen_pairs and not self.planned_executions) or item.execution_id in seen_ids:
                raise SharedControlError("Repeated case/protocol or execution IDs require the planned per-execution extension.")
            seen_pairs.add(pair)
            seen_ids.add(item.execution_id)
            if item.product_status == "not_executed":
                item.case_status = item.target_status = "not_executed"
            elif (item.product_status == "failed" or item.tool_status == "failed" or item.target_status == "failed"
                    or item.deterministic_status == "failed" or item.content_status == "failed"
                    or "failed" in item.critical_contracts.values()):
                item.case_status = "failed"
            elif (item.target_status == "passed" and item.content_status == "passed" and item.evidence_status == "complete"
                    and item.semantic_status == "passed" and all(value in {"passed", "not_applicable"} for value in item.critical_contracts.values())):
                item.case_status = "passed"
            else:
                item.case_status = "pending_review"
        cases = [{**vars(item), "evidence": {name: str(path.relative_to(self.result_path.parent))
                  if path.is_relative_to(self.result_path.parent) else str(path) for name, path in item.evidence.items()}}
                 for item in self.cases]
        measured = {(item.case_id, item.protocol): item for item in self.cases}
        measured_executions = {item.execution_id: item for item in self.cases}
        coverage = []
        for row in self.catalogue:
            for protocol in row["protocols"]:
                outcome = measured.get((row["case_id"], protocol))
                planned_rows = [item for item in self.planned_executions if item.case_id == row["case_id"] and item.protocol == protocol]
                observed = [measured_executions[item.execution_id] for item in planned_rows if item.execution_id in measured_executions]
                status = outcome.product_status if outcome is not None else "not_executed"
                real = outcome is not None and outcome.facts.get("real_http_attempts", 0) > 0
                if planned_rows:
                    states = [measured_executions[item.execution_id].product_status if item.execution_id in measured_executions
                        else "not_executed" for item in planned_rows]
                    status = ("failed" if "failed" in states or any(item.tool_status == "failed" for item in observed)
                        else "not_executed" if all(value == "not_executed" for value in states)
                        else "inconclusive" if any(value in {"inconclusive","not_executed"} for value in states) else "passed")
                    real = any(item.facts.get("real_http_attempts",0) > 0 for item in observed)
                coverage.append({"case_id": row["case_id"], "protocol": protocol, "group": row["group"],
                    "observation_layer": row["observation_layer"], "expectation": row["expectation"],
                    "offline": status if (outcome is not None or planned_rows) and not real else "not_measured",
                    "current_real": status if real else "not_measured",
                    **history.coverage(row["case_id"], protocol),
                    "semantic": outcome.semantic_status if outcome is not None else "not_measured",
                    "execution": status if outcome is not None or planned_rows else "not_selected",
                    "executions": [{"execution_id": execution.execution_id,"stage":execution.stage,"scenario_id":execution.scenario_id,
                        "material_variant":execution.material_variant,"risk_branch":execution.risk_branch,
                        "status": measured_executions[execution.execution_id].execution_status if execution.execution_id in measured_executions else "not_executed",
                        "reason":measured_executions[execution.execution_id].facts.get("reason") if execution.execution_id in measured_executions else "not started"}
                        for execution in self.planned_executions if execution.case_id == row["case_id"] and execution.protocol == protocol]})
        self.result_path.write_text(json.dumps({"run_id": self.run_id, "mode": mode, "version": self.version,
            "coverage": coverage,
            "coverage_denominators":{**{stage:sum(item.stage == stage for item in self.planned_executions) +
                sum(alias.stage == stage for item in self.planned_executions for alias in item.shared_with)
                for stage in ("screening","quality","judge")},
                "product_executions":sum(item.role == "product" for item in self.planned_executions)},
            "planned_denominator": len(self.planned_executions) if self.planned_executions else len(self.cases),
            "planned_executions": [item.model_dump(mode="json") for item in self.planned_executions], "historical_observations": history.report(),
            "historical_reuse": historical_group_reports(self.historical_groups),
            "measurements": {"offline": "executed" if any(item.facts.get("mock_http_attempts",0) > 0 for item in self.cases) or mode == "offline" else "not_measured",
                "historical_real": "not_measured", "current_real": "executed" if any(
                    item.facts.get("real_http_attempts", 0) > 0 for item in self.cases) else "not_measured",
                "semantics": "not_measured", "cache_benefit": "not_measured"}, "cases": cases},
            default=str, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _run_plan(plan: AcceptancePlan, *, root: Path, registry: CaseRegistry,
                   session_factory: "SessionFactory | None" = None, live_gate: "LiveGate | None" = None,
                   execution_ids: tuple[str, ...] | None = None) -> RunResult:
    from tests.support.agent_acceptance.preparation import CaseBudgetExceeded
    from tests.support.agent_acceptance.historical_reuse import historical_group_reports
    historical_group_reports(plan.historical_groups)
    selected = registry.select(plan)
    if plan.mode == "prepare-live" or plan.mode == "live" and live_gate is None:
        raise SharedControlError("Use prepare_live or the authorized live handoff; no connection was acquired.")
    if plan.mode == "live":
        assert live_gate is not None
        for case, protocol in selected:
            if not case.formal_live:
                raise SharedControlError("This case has no authorized formal service adapter; no connection was acquired.")
    from tests.support.agent_acceptance.reporting import assess, publish_evidence, version_facts
    root.mkdir(parents=True, exist_ok=False)
    result = RunResult(uuid4().hex, [], root / "result.json", version_facts(plan, registry), registry.catalogue(), plan.executions, plan.historical_groups)
    parent_gate = live_gate
    for index, (case, protocol) in enumerate(selected):
        execution = plan.executions[index] if plan.executions else None
        live_gate = parent_gate.for_execution(execution.execution_id) if parent_gate is not None and execution is not None else parent_gate
        directory = root / case.case_id / protocol
        if execution is not None:
            directory = directory / execution.directory_name
        directory.mkdir(parents=True)
        recognized_permission_stop = False
        outcome: CaseOutcome | None = None
        try:
            previous = live_gate.recorded_outcome() if live_gate is not None and execution is not None else None
            if previous is not None:
                result.cases.append(previous)
                result.save(plan.mode)
                continue
            if execution is not None and execution_ids is not None and execution.execution_id not in execution_ids:
                result.cases.append(CaseOutcome("not_executed", {"reason": "stage not selected; remains eligible"},
                    case_id=case.case_id, protocol=protocol, execution_id=execution.execution_id,
                    expectation_status="not_executed", execution_status="not_executed"))
                result.save(plan.mode)
                continue
            prior_dependencies = []
            if parent_gate is not None and execution is not None:
                prior_dependencies = [parent_gate.for_execution(identity).recorded_outcome() for identity in execution.dependencies]
            dependency_results = [*result.cases, *(item for item in prior_dependencies if item is not None)]
            blocked = [name for name in (execution.dependencies if execution else case.dependencies) if not any(
                (item.execution_id == name if execution else item.case_id == name and item.protocol == protocol)
                and item.product_status == "passed" and item.tool_status == "passed" for item in dependency_results)]
            if live_gate is not None:
                live_gate.validate(case_id=case.case_id, protocol=protocol)
            if blocked:
                outcome = CaseOutcome("not_executed", {"reason": "dependency did not pass", "dependencies": blocked})
            else:
                context = CaseContext(directory, protocol, result.run_id, case.case_id,
                    session_factory=session_factory, mode=plan.mode, live_gate=live_gate, execution=execution,
                    dependency_evidence={item.execution_id:dict(item.evidence) for item in dependency_results
                        if execution is not None and item.execution_id in execution.dependencies})
                with live_gate.running(context.cancellation_token) if live_gate is not None else nullcontext():
                    if plan.mode == "live" and case.case_id != "desktop.handoff":
                        from tests.support.agent_acceptance.formal import handoff
                        outcome = await handoff(context)
                    else:
                        outcome = await case.run(context)
                if live_gate is not None:
                    control = live_gate.accounting_report()
                    if control["stopped"]:
                        if (plan.quality_packet is not None and case.case_id.startswith("quality.")
                                and outcome.product_status == "failed"
                                and (outcome.tool_status == "passed" or (
                                    outcome.facts.get("desktop_exit_passed") is False
                                    and outcome.facts.get("processes_complete") is True
                                    and outcome.facts.get("tool_contract_passed") is True
                                    and not outcome.facts.get("driver_errors")))
                                and outcome.facts.get("known_default_permission_loss")):
                            from tests.support.agent_acceptance import builtin_registry
                            from tests.support.agent_acceptance.quality_runtime import recognize_default_permission_loss
                            canonical = builtin_registry(quality_packet=plan.quality_packet)
                            originals = [json.loads(path.read_bytes()) for path in directory.glob("*/receiver/exchange.json")]
                            proof = recognize_default_permission_loss(case.case_id, directory, protocol, ledger=control, exchanges=originals)
                            recognized_permission_stop = canonical.cases.get(case.case_id) == case and proof is not None and proof == outcome.facts["known_default_permission_loss"]
                        if not recognized_permission_stop:
                            raise SharedControlError(control["stop_reason"] or "Shared control stopped.")
                    if not recognized_permission_stop:
                        for label, path in outcome.evidence.items():
                            if label == "conversation" or label.startswith("records/") and label.count("/") == 1 and path.suffix == ".json":
                                live_gate.observe_product_stop(path, case_id=case.case_id, protocol=protocol)
                if execution is not None:
                    outcome.execution_id = execution.execution_id
                outcome.facts.setdefault("source_result",str(result.result_path))
                publish_evidence(context, outcome, case)
            outcome.case_id, outcome.protocol = case.case_id, protocol
            assess(case, outcome)
        except CaseBudgetExceeded as error:
            started = execution is None or live_gate is not None and any(item["execution_id"] == execution.execution_id
                for item in live_gate.accounting_report()["attempts"])
            stop = live_gate.accounting_report()["case_stops"].get(live_gate.stop_key(case.case_id, protocol), {}) if live_gate is not None else {}
            outcome = CaseOutcome("inconclusive" if started else "not_executed", {"reason": str(error), **stop,
                "unfinished": "Remaining work was not authorized to send."},
                expectation_status="unmet" if started else "not_executed")
        except SharedControlError as error:
            failure = directory / "control-failure.txt"
            failure.write_text(str(error) + "\n", encoding="utf-8")
            rejected = CaseOutcome("failed", {"error": str(error)}, {"failure": failure},
                tool_status="failed", case_id=case.case_id, protocol=protocol,
                execution_id=execution.execution_id if execution else "")
            if isinstance(error, IncompleteEvidenceError) and error.stop_violation is not None:
                rejected.critical_contracts["K3"] = "failed"
                rejected.facts.update(request_stop_status=error.stop_violation.request_status,
                                      action_stop_status=error.stop_violation.action_status)
                rejected.evidence_status = "incomplete"
            result.cases.append(rejected)
            result.cases.extend(CaseOutcome("not_executed", {"reason": "shared control failed"},
                case_id=remaining.case_id, protocol=item, expectation_status="not_executed",
                execution_id=plan.executions[position].execution_id if plan.executions else "")
                for position, (remaining, item) in enumerate(selected[index + 1:], index + 1))
            result.save(plan.mode)
            if live_gate is not None:
                try:
                    live_gate.stop()
                except SharedControlError as stop_error:
                    result.cases[index].facts["stop_record_error"] = str(stop_error)
                    result.save(plan.mode)
            raise
        except Exception as error:
            failure = directory / "failure.txt"
            failure.write_text(traceback.format_exc(), encoding="utf-8")
            outcome = CaseOutcome("failed", {"error": str(error), "error_type": type(error).__name__},
                                  {"failure": failure}, tool_status="failed")
        outcome.case_id, outcome.protocol = case.case_id, protocol
        if live_gate is not None:
            report = live_gate.accounting_report()
            stop = report["case_stops"].get(live_gate.stop_key(case.case_id, protocol))
            outcome.facts["budget"] = {**report, **(stop or {})}
            outcome.execution_id = live_gate.execution_id(case.case_id, protocol)
        if execution is not None:
            outcome.execution_id = execution.execution_id
        result.cases.append(outcome)
        result.save(plan.mode)
        if live_gate is not None and execution is not None:
            live_gate.finish_execution(outcome)
        if recognized_permission_stop:
            for position, (remaining, item) in enumerate(selected[index + 1:], index + 1):
                remaining_execution = plan.executions[position] if plan.executions else None
                previous = parent_gate.for_execution(remaining_execution.execution_id).recorded_outcome() if parent_gate is not None and remaining_execution is not None else None
                result.cases.append(previous if previous is not None else CaseOutcome("not_executed",
                    {"reason": "Shared controls remain stopped after the independently verified product default-permission loss.", "known_product_gap": "public_new_default_permissions_lost"},
                    case_id=remaining.case_id, protocol=item, execution_id=remaining_execution.execution_id if remaining_execution else "",
                    expectation_status="not_executed", execution_status="not_executed"))
            result.save(plan.mode)
            return result
    return result


async def run_plan(plan: AcceptancePlan, *, root: Path, registry: CaseRegistry,
                   session_factory: "SessionFactory | None" = None, live_gate: "LiveGate | None" = None,
                   execution_ids: tuple[str, ...] | None = None) -> RunResult:
    plan = AcceptancePlan.model_validate(plan.model_dump())
    if execution_ids is not None and (not plan.executions or not execution_ids
            or len(execution_ids) != len(set(execution_ids))
            or not set(execution_ids) <= {item.execution_id for item in plan.executions}):
        raise ValueError("Stage selection requires unique original frozen execution IDs.")
    if live_gate is not None:
        from tests.support.agent_acceptance.preparation import CaseBudgetExceeded
        live_gate.validate_plan(plan)
        selected = registry.select(plan)
        with live_gate.owner():
            for index, (case, protocol) in enumerate(selected):
                if execution_ids is not None and plan.executions[index].execution_id not in execution_ids:
                    continue
                try:
                    view = live_gate.for_execution(plan.executions[index].execution_id) if plan.executions else live_gate
                    view.validate(case_id=case.case_id, protocol=protocol)
                except CaseBudgetExceeded:
                    pass
            return await _run_plan(plan, root=root, registry=registry, session_factory=session_factory, live_gate=live_gate, execution_ids=execution_ids)
    return await _run_plan(plan, root=root, registry=registry, session_factory=session_factory, live_gate=live_gate, execution_ids=execution_ids)
