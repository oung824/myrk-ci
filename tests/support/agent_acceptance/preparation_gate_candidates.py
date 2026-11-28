"""Freeze and activate a reviewed replacement for a prepared candidate."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tests.support.agent_acceptance.core import AcceptancePlan, SharedControlError
from tests.support.agent_acceptance.eligibility import RouteStandardVersion, preparation_eligibility
from tests.support.agent_acceptance.preparation_gate_validation import (
    frozen_evaluator_isolation_directories,
    normalize_evaluator_isolation_directories,
)
from tests.support.agent_acceptance.preparation_models import (
    CandidateFreezeReview,
    LiveAuthorization,
    PreparedBundle,
    SOURCE,
    bundle_fingerprint,
    digest,
)
from tests.support.agent_acceptance.preparation_snapshot import (
    bound_files,
    bundle_material_root,
    candidate_plan_matches,
    read_prepared_snapshot,
)
from tests.support.agent_acceptance.preparation_transaction import ledger_mutation
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from tests.support.agent_acceptance.core import CaseRegistry
    from tests.support.agent_acceptance.preparation import LiveGate
    from tests.support.agent_acceptance.preparation_models import _LiveLedger
    from tests.support.agent_acceptance.preparation_transaction import _LedgerTransactionHost
    from typing import Protocol

    class _CandidateGateHost(_LedgerTransactionHost, Protocol):
        root: Path
        registry: CaseRegistry
        authorization: LiveAuthorization | None

        def _read_ledger(self) -> _LiveLedger: ...
        def _save_ledger(self, ledger: _LiveLedger) -> None: ...
        def _parent_binding(self, bundle: PreparedBundle, grant: LiveAuthorization, *, activate: bool, required: bool) -> None: ...


def _bundle_evaluator_isolation_directories(bundle: PreparedBundle, *, required: bool = False) -> tuple[str, ...] | None:
    if bundle.plan.quality_packet is None:
        return None
    case_ids = {row.case_id for row in bundle.plan.executions} if bundle.plan.executions else set(bundle.plan.cases)
    declarations = {
        frozen_evaluator_isolation_directories(bundle, case_id) for case_id in case_ids if case_id.startswith("quality.")
    }
    if len(declarations) > 1 or (required and None in declarations):
        raise SharedControlError("All frozen quality cases require the same explicit evaluator isolation directory list.")
    return next(iter(declarations), None)


class _CandidateGate:
    def propose_candidate(self: "_CandidateGateHost", *, new_plan: AcceptancePlan | None = None,
                          evaluator_isolation_directories: list[str] | tuple[str, ...] | None = None) -> PreparedBundle:
        """Freeze a closed draft under the same root; this grants no sending authority."""
        from uuid import uuid4
        from tests.support.agent_acceptance.preparation import _prepare_case_executions
        from tests.support.agent_acceptance.reporting import environment_facts
        original = read_prepared_snapshot(self.root,
            binding=self.authorization.binding if self.authorization is not None else None)
        if new_plan is not None and not candidate_plan_matches(original, new_plan):
            raise SharedControlError("Candidate plan substitution changes the original frozen identity.")
        if original.plan.parent_budget is None or not original.plan.executions:
            raise SharedControlError("Candidate freeze requires the original frozen parent.")
        previous_isolation = _bundle_evaluator_isolation_directories(original)
        isolation = (previous_isolation if evaluator_isolation_directories is None
            else normalize_evaluator_isolation_directories(evaluator_isolation_directories))
        if previous_isolation is not None and isolation != previous_isolation:
            raise SharedControlError("Candidate cannot replace the original evaluator isolation directory list.")
        deepseek_standard: RouteStandardVersion = next((route.standard_version for route in original.eligibility
            if route.protocol == "deepseek" and route.role == "product"), "2026-11-16")
        plan = new_plan or original.plan
        registry = self.registry
        if plan.quality_packet is not None:
            from tests.support.agent_acceptance import builtin_registry
            registry = builtin_registry(quality_packet=plan.quality_packet)
        directory = uuid4().hex
        content = self.root / ".candidates" / directory
        content.mkdir(parents=True, exist_ok=False)
        execution = _prepare_case_executions(plan, content, registry, isolation)
        bundle = original.model_copy(update={"source_root": SOURCE, "candidate_directory": directory,
            "plan": plan, "files": bound_files(plan, registry), "execution": execution,
            "environment": environment_facts(),
            "eligibility": preparation_eligibility(deepseek_standard=deepseek_standard), "binding": ""})
        bundle.binding = bundle_fingerprint(bundle)
        for name in bundle.files:
            target = content / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((SOURCE / name).read_bytes())
        (content / "prepared.json").write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return bundle

    @ledger_mutation
    def refreeze_candidate(self: "_CandidateGateHost", candidate: PreparedBundle, *, review: Path, review_sha256: str,
                           grant: LiveAuthorization) -> "LiveGate":
        """Activate a reviewed draft without changing genesis grant, counters or stops."""
        ledger = self._read_ledger()
        previous = read_prepared_snapshot(self.root,
            binding=self.authorization.binding if self.authorization is not None else None)
        parent = previous.plan.parent_budget
        current = ledger.candidates[-1]["candidate_binding"] if ledger.candidates else ledger.binding
        if (parent is None or self.authorization is None or previous.binding != current
                or candidate.root != self.root.resolve() or candidate.candidate_directory is None
                or not candidate_plan_matches(previous, candidate.plan, material_root=bundle_material_root(candidate))
                or candidate.budget_proposals != previous.budget_proposals
                or candidate.prices != previous.prices or candidate.sending_enabled
                or candidate.binding != bundle_fingerprint(candidate) or ledger.stopped
                or any(item.state in {"reserved", "http_started"} for item in ledger.attempts)):
            raise SharedControlError("Candidate freeze changed parent/plan/caps or has a stopped/inflight predecessor.")
        previous_isolation = _bundle_evaluator_isolation_directories(previous)
        candidate_isolation = _bundle_evaluator_isolation_directories(candidate, required=True)
        if previous_isolation is not None and candidate_isolation != previous_isolation:
            raise SharedControlError("Candidate cannot replace the original evaluator isolation directory list.")
        content = bundle_material_root(candidate)
        registry = self.registry
        if candidate.plan.quality_packet is not None:
            from tests.support.agent_acceptance import builtin_registry
            registry = builtin_registry(quality_packet=candidate.plan.quality_packet)
        if (any(part.is_symlink() or part.is_junction() for part in (content, *content.parents))
                or PreparedBundle.model_validate_json((content / "prepared.json").read_bytes()) != candidate
                or bound_files(candidate.plan, registry) != candidate.files
                or any(sha256((content / name).read_bytes()).hexdigest() != expected for name, expected in candidate.files.items())):
            raise SharedControlError("Proposed candidate bytes changed; no connection acquired.")
        if not review.resolve().is_relative_to(self.root.resolve()) or not review.is_file() or sha256(review.read_bytes()).hexdigest() != review_sha256:
            raise SharedControlError("Candidate freeze requires its exact preserved review original.")
        decision = CandidateFreezeReview.model_validate_json(review.read_bytes())
        rows = {item.execution_id: item for item in previous.plan.executions}
        tuning = {name for name,item in rows.items() if item.stage == "quality" and ".tuning." in item.scenario_id}
        affected = set(decision.affected_execution_ids)
        if (decision.parent_id != parent.parent_id or decision.accounting_root.resolve() != self.root.resolve()
                or decision.previous_binding != previous.binding or decision.candidate_binding != candidate.binding
                or not affected or len(affected) != len(decision.affected_execution_ids) or not affected <= set(rows)
                or set(decision.tuning_execution_ids) != tuning or not tuning
                or any(name not in ledger.execution_results for name in tuning)
                or decision.tuning_results != {name: sha256(json.dumps(ledger.execution_results[name], sort_keys=True).encode()).hexdigest() for name in tuning}
                or decision.untuned_materials != {name: rows[name].material_sha256 for name in affected}
                or any(rows[name].stage != "quality" or ".formal." not in rows[name].scenario_id for name in affected)
                or any(name in ledger.execution_results or any(item.execution_id == name for item in ledger.attempts) for name in affected)
                or any(name in ledger.case_stops for name in affected)
                or any(not item["expected_block"] and affected.intersection(item.get("execution_ids", [])) for item in ledger.scope_stops)
                or any(any(key.startswith(name + ":") for key in ledger.product_task_stops) for name in affected)):
            raise SharedControlError("Candidate review lacks closed tuning/unseen formal originals or revives a stopped scope.")
        original_grant = ledger.authorization or self.authorization
        if (grant.binding != candidate.binding or grant.model_dump(exclude={"binding", "authorization_id"}) != original_grant.model_dump(exclude={"binding", "authorization_id"})
                or grant.authorization_id in {original_grant.authorization_id, *(item["grant"]["authorization_id"] for item in ledger.candidates)}
                or (grant.transport == "mock") != (decision.review_kind == "synthetic_control")):
            raise SharedControlError("Candidate grant changes original authorization caps or identity.")
        self._parent_binding(previous, self.authorization, activate=not ledger.attempts, required=bool(ledger.attempts))
        if ledger.authorization is None:
            ledger.authorization = self.authorization
            ledger.authorization_binding = digest(self.authorization.model_dump(mode="json"))
        saved_review = content / "candidate-review.json"
        with saved_review.open("xb") as output:
            output.write(review.read_bytes())
        ledger.candidate_codec = "active-candidates-v1"
        ledger.candidates.append({"previous_binding": previous.binding, "candidate_binding": candidate.binding,
            "directory": candidate.candidate_directory, "grant": grant.model_dump(mode="json"),
            "review_path": saved_review.relative_to(self.root).as_posix(), "review_sha256": review_sha256,
            "affected_execution_ids": sorted(affected), "recorded_at": datetime.now(timezone.utc).isoformat(),
            "before_request": len(ledger.attempts) + 1})
        ledger.active_candidates.update({name: candidate.binding for name in affected})
        self._save_ledger(ledger)
        return replace(cast("LiveGate", self), registry=registry, authorization=grant)
