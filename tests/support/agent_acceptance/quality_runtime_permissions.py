from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.quality_packet import PACKETS

if TYPE_CHECKING:
    from tests.support.agent_acceptance.preparation_models import PreparedBundle


def required_denials(
    root: Path, *, source_roots: tuple[Path, ...] = (), evidence_root: Path | None = None,
    evaluator_isolation_directories: tuple[str, ...] = (),
) -> set[str]:
    """Reconstruct the tool ceiling from controlled roots, never a runtime claim."""
    from myrk.project_paths import project_root
    from tests.support.agent_acceptance.preparation_gate_validation import evaluator_isolation_paths

    common = project_root(SOURCE)
    registered = (*source_roots, evidence_root) if evidence_root is not None else source_roots
    if any(not path.is_absolute() or not path.resolve().is_relative_to(common) for path in registered):
        raise ValueError("Quality evaluator copy roots must stay inside the local project.")
    roots = {SOURCE, *source_roots}
    if root.parent.name == ".scratch" and root.name.startswith("execution-"):
        roots.add(root.parent.parent)
    denied = {str(root / "quality-task.json"), str(root / "review-only"), str(root / "quality-review"), str(root / "review-context"),
        *(str(root / name) for name in ("original-before.json", "pending-original.json", "original-parent.json", "original-child.json", "quality-permission-change.json", "quality-permission-initial.original.json", "quality-permission-new.original.json", "legacy-source-v5.json", "recovery-observation.json", "reliable-original.json", "effect-at-boundary.txt", "save-boundary.json")),
        str(common / ".git"), str(common / ".scratch/worktrees"), str(common / ".scratch/check-runs/results"),
        str(common / ".scratch/retro-agent-live-acceptance-20261116"), str(common / ".scratch/quality-material-usage"),
        str(common / "tests/acceptance/materials")}
    if evidence_root is not None:
        denied.add(str(evidence_root))
    for source in roots:
        denied.update((str(source / "tests"), str(source / ".git"), str(source / ".candidates"),
            str(source / PACKETS),
            str(source / ".scratch/retro-agent-live-acceptance-20261116/implementation")))
    denied.update(str(path) for path in evaluator_isolation_paths(evaluator_isolation_directories))
    return denied


def verify_frozen_isolation(case_id: str, root: Path, bundle: PreparedBundle, *, evidence_root: Path) -> None:
    from tests.support.agent_acceptance.core import SharedControlError
    from tests.support.agent_acceptance.preparation_gate_validation import frozen_evaluator_isolation_directories
    from tests.support.agent_acceptance.preparation_snapshot import bundle_material_root
    from tests.support.agent_acceptance.quality_runtime import BoundQualityTask

    directories = frozen_evaluator_isolation_directories(bundle, case_id)
    if directories is None:
        raise SharedControlError("Canonical quality execution lacks its frozen evaluator isolation declaration.")
    roots = (bundle.source_root, bundle_material_root(bundle), bundle.root)
    try:
        bound = BoundQualityTask.model_validate_json((root / "quality-task.json").read_bytes())
        denied = required_denials(root, source_roots=roots, evidence_root=evidence_root,
            evaluator_isolation_directories=directories)
    except (OSError, ValueError) as error:
        raise SharedControlError(f"Quality runtime isolation cannot be verified: {error}") from error
    if bound.evidence_root != evidence_root:
        raise SharedControlError("Quality runtime evidence isolation differs from the launcher binding.")
    if (bound.packet != bundle.plan.quality_packet or bound.source_roots != roots
            or bound.evaluator_isolation_directories != directories or set(bound.denied_paths) != denied):
        raise SharedControlError("Quality runtime isolation differs from the frozen evaluator isolation declaration.")



def permission_expectations(case_id: str, root: Path, protocol: Any) -> tuple[Any, Any, tuple[Path, ...]]:
    from myrk.conversation import TaskScope
    from myrk.permissions import ApprovalSettings
    from tests.support.agent_acceptance.quality_runtime import bound_task, handler, manifest
    request = manifest(case_id, root, protocol)
    _, visible, _ = bound_task(case_id, root)
    module = handler(visible)
    scope = TaskScope.model_validate(request["scope"])
    roots = tuple(Path(name).resolve() for name in module.allowed_task_roots(visible, root=root)) if hasattr(module, "allowed_task_roots") else (Path(scope.root).resolve(),)
    if not roots or any(not path.is_relative_to(root.resolve()) for path in roots):
        raise ValueError("Canonical permitted Task roots must stay inside their actual scenario runtime.")
    return ApprovalSettings.model_validate(request["approval"]), scope, roots



def task_permission_gaps(task: Any, expected: Any, scope: Any, allowed_roots: tuple[Path, ...], *, execution_scope: Any = None) -> list[str]:
    """One predicate for pre-send enforcement and later original-record scoring."""
    actual = task.approval
    gaps = []
    writable = [Path(name).resolve() for name in expected.access.writable_roots]
    if actual.access.sandbox != "restricted": gaps.append("restricted_access_removed")
    if actual.mode != expected.mode or actual.revision != expected.revision or actual.condition != expected.condition or actual.reviewer != expected.reviewer or actual.review_mandatory != expected.review_mandatory:
        gaps.append("canonical_approval_changed")
    if not set(expected.access.denied_paths) <= set(actual.access.denied_paths): gaps.append("canonical_tool_denials_removed")
    if expected.access.read_only and not actual.access.read_only: gaps.append("read_only_ceiling_removed")
    if any(not any(Path(name).resolve().is_relative_to(parent) for parent in writable) for name in actual.access.writable_roots): gaps.append("writable_scope_expanded")
    if actual.access.network != expected.access.network: gaps.append("network_ceiling_changed")
    for active in (task.scope, execution_scope):
        if active is None:
            if task.scope is None: gaps.append("missing_actual_task_scope")
            continue
        if not any(Path(active.root).resolve().is_relative_to(parent) for parent in allowed_roots): gaps.append("task_root_outside_canonical_scope")
        if active.provider != scope.provider or active.recipients != scope.recipients: gaps.append("task_recipient_changed")
        if not set(active.allowed_actions) <= set(scope.allowed_actions): gaps.append("task_actions_expanded")
        if any(check not in scope.checks for check in active.checks): gaps.append("task_checks_expanded")
    return sorted(set(gaps))



def product_permission_status(case_id: str, root: Path, protocol: Any, records: list[dict[str, Any]]) -> dict[str, Any]:
    from myrk.conversation import ConversationRecord
    expected, scope, roots = permission_expectations(case_id, root, protocol)
    evaluated, gaps, missing = set(), [], []
    for value in records:
        try:
            record = ConversationRecord.model_validate(value)
        except ValueError:
            missing.append("invalid_product_record")
            continue
        if not record.turns: missing.append("product_turns")
        for turn in record.turns:
            if turn.task is None:
                missing.append("product_task")
                continue
            evaluated.add(turn.task.id)
            gaps.extend(task_permission_gaps(turn.task, expected, scope, roots, execution_scope=turn.execution_scope))
    if not evaluated: missing.append("actual_product_task_permissions")
    return {"status": "failed" if gaps else "unknown" if missing else "passed", "violations": sorted(set(gaps)),
        "missing_evidence": sorted(set(missing)), "evaluated_task_ids": sorted(evaluated)}



def verify_product_permissions(case_id: str, root: Path, protocol: Any, records_root: Path, attempt: Any) -> None:
    """Bind the actual saved native RequestAttempt to its enforced Task ceiling."""
    from myrk.conversation import ConversationRecord
    from tests.support.agent_acceptance.core import SharedControlError
    if not records_root.resolve().is_relative_to(root.resolve()):
        raise SharedControlError("Quality native product records are outside their actual scenario root.")
    expected, scope, roots = permission_expectations(case_id, root, protocol)
    matches = []
    identity = (attempt.number, attempt.step_id, attempt.actor, attempt.purpose, attempt.delegation_id)
    for path in records_root.rglob("*.json"):
        if path.parent != records_root and not path.parent.name.endswith("-delegations"):
            continue
        record = ConversationRecord.model_validate_json(path.read_bytes())
        for turn in record.turns:
            requests = [*turn.request_attempts, *(request for action in turn.actions if action.review is not None for request in action.review.request_attempts)]
            if turn.task is not None and any((item.number, item.step_id, item.actor, item.purpose, item.delegation_id) == identity for item in requests):
                matches.append(turn)
    if not matches or len({turn.task.id for turn in matches if turn.task is not None}) != 1:
        raise SharedControlError("Quality native request does not resolve to one actual saved product Task permission record.")
    if any(task_permission_gaps(turn.task, expected, scope, roots, execution_scope=turn.execution_scope) for turn in matches):
        raise SharedControlError("Quality actual product Task permissions changed; canonical tool denials and restricted access must survive every work or child request.")



def recognize_default_permission_loss(case_id: str, root: Path, protocol: Any, *, ledger: dict[str, Any], exchanges: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Classify one proven product default-policy loss; never grants or resumes sends."""
    from myrk.conversation import ConversationRecord, TaskScope
    from myrk.permissions import ApprovalSettings
    from tests.support.agent_acceptance.quality_runtime import bound_task, handler, portable_task_path
    try:
        proof_path = root / "quality-permission-change.json"
        proof = json.loads(proof_path.read_bytes())
        if (proof.get("schema_version") != 1 or proof.get("origin") != "public_new_after_temporary_configuration"
                or proof.get("explicit_mode_negative") is not False):
            return None
        task, visible, bound = bound_task(case_id, root)
        if task.group not in {"QG4", "QG5"} or visible.parameters.get("implementation_negative"):
            return None
        source_files = {"quality-permission-change.json": proof_path}
        originals = []
        for label in ("initial", "new"):
            relative = Path(proof[label + "_original"])
            if not portable_task_path(relative.as_posix()) or len(relative.parts) != 1:
                return None
            original = root / relative
            source_files[relative.name] = original
            record = ConversationRecord.model_validate_json(original.read_bytes())
            if not record.turns or record.turns[-1].task is None:
                return None
            originals.append(record.turns[-1])
        first, changed = originals
        assert first.task is not None and changed.task is not None
        new_event = proof["actual_new_event"]
        initial_path, new_path = Path(proof["initial_path"]), Path(proof["new_path"])
        if (new_event.get("event") != "conversation" or Path(new_event.get("path", "")) != new_path
                or initial_path == new_path or first.task.id == changed.task.id
                or (proof["initial_task_id"], proof["new_task_id"]) != (first.task.id, changed.task.id)
                or changed.status != "failed"):
            return None
        scenarios = list(root.glob("*/scenario.json"))
        if not scenarios:
            return None
        scenario_path = next((path for path in scenarios if json.loads(path.read_bytes()).get("case_id") == case_id), None)
        if scenario_path is None:
            return None
        scenario = json.loads(scenario_path.read_bytes())
        source_files[scenario_path.relative_to(root).as_posix()] = scenario_path
        original_root = Path(scenario["root"])
        original_records = Path(scenario["records"])
        if (not initial_path.is_relative_to(original_records) or not new_path.is_relative_to(original_records)
                or not original_records.is_relative_to(original_root) or scenario.get("implementation_negative")):
            return None
        saved_time_only = {}
        for label, declared in (("initial", initial_path), ("new", new_path)):
            actual_relative = declared.relative_to(original_root)
            actual_source = root / actual_relative
            actual_raw = actual_source.read_bytes()
            captured_raw = (root / proof[label + "_original"]).read_bytes()
            actual_record = ConversationRecord.model_validate_json(actual_raw)
            captured_record = ConversationRecord.model_validate_json(captured_raw)
            if actual_record.model_dump(exclude={"last_saved_at"}) != captured_record.model_dump(exclude={"last_saved_at"}):
                return None
            actual_json, captured_json = json.loads(actual_raw), json.loads(captured_raw)
            actual_saved, captured_saved = actual_json.pop("last_saved_at", None), captured_json.pop("last_saved_at", None)
            if actual_json != captured_json:
                return None
            if actual_raw != captured_raw:
                if actual_saved == captured_saved:
                    return None
                saved_time_only[label] = {"captured_last_saved_at": captured_saved, "actual_last_saved_at": actual_saved,
                    "comparison": "All typed and raw JSON record fields except last_saved_at are equal; normal shutdown re-saved the session."}
            source_files[actual_relative.as_posix()] = actual_source
        event_sources = list(root.glob("*/receiver/events.json"))
        raw_events: list[dict[str, Any]] = []
        for path in event_sources:
            recorded = json.loads(path.read_bytes())
            raw_events.extend(recorded if isinstance(recorded, list) else recorded.get("events", []))
            source_files[path.relative_to(root).as_posix()] = path
        if proof["actual_new_event"] not in raw_events or proof["failed_event"] not in raw_events:
            return None
        new_index, failed_index = raw_events.index(proof["actual_new_event"]), raw_events.index(proof["failed_event"])
        initial_indices = [index for index, event in enumerate(raw_events)
            if event.get("event") == "started" and event.get("task_id") == first.task.id and Path(event.get("path", "")) == initial_path]
        if not initial_indices or not min(initial_indices) < new_index < failed_index:
            return None
        expected = ApprovalSettings.model_validate(scenario["approval"])
        if (first.task.approval != expected or expected.access.sandbox != "restricted"
                or not required_denials(original_root, source_roots=bound.source_roots, evidence_root=bound.evidence_root,
                    evaluator_isolation_directories=bound.evaluator_isolation_directories or ()) <= set(expected.access.denied_paths)):
            return None
        changed_approval = changed.task.approval
        if (changed_approval.mode != "request_approval" or changed_approval.access.sandbox != "restricted"
                or changed_approval.revision == expected.revision
                or set(expected.access.denied_paths) <= set(changed_approval.access.denied_paths)
                or changed_approval.access.network != expected.access.network):
            return None
        expected_scope = TaskScope.model_validate(scenario["scope"])
        module = handler(visible)
        allowed_roots = tuple(Path(name).resolve() for name in module.allowed_task_roots(visible, root=original_root)) if hasattr(module, "allowed_task_roots") else (Path(expected_scope.root).resolve(),)
        active_scope = changed.task.scope
        if (active_scope is None or active_scope.provider != expected_scope.provider or active_scope.recipients != expected_scope.recipients
                or not set(active_scope.allowed_actions) <= set(expected_scope.allowed_actions)
                or any(check not in expected_scope.checks for check in active_scope.checks)
                or not any(Path(active_scope.root).resolve().is_relative_to(path) for path in allowed_roots)):
            return None
        failed_event = proof["failed_event"]
        if (failed_event.get("event") != "finished" or Path(failed_event.get("path", "")) != new_path
                or failed_event.get("turn", {}).get("task", {}).get("id") != changed.task.id
                or failed_event.get("turn", {}).get("status") != "failed"):
            return None
        guard_error = "Quality actual product Task permissions changed; canonical tool denials and restricted access must survive every work or child request."
        transports = list(root.glob("*/receiver/transport.json"))
        if (not ledger.get("stopped") or guard_error not in str(ledger.get("stop_reason", ""))
                or not any(guard_error in json.loads(path.read_bytes()).get("control_error_history", []) for path in transports)):
            return None
        first_steps = {attempt.step_id for attempt in first.request_attempts if attempt.step_id}
        new_steps = {attempt.step_id for attempt in changed.request_attempts if attempt.step_id}
        actual = [attempt for attempt in ledger.get("attempts", []) if attempt.get("case_id") == case_id and attempt.get("protocol") == protocol]
        if (not first_steps or not new_steps or any(attempt.get("product_step_id") in new_steps for attempt in ledger.get("attempts", []))
                or not actual or any(attempt.get("product_step_id") not in first_steps for attempt in actual)):
            return None
        actual_http = sum(attempt.get("state") in {"http_started", "response_complete", "unknown"} for attempt in actual)
        wire_http = sum(not row.get("not_sent", False) for envelope in exchanges for row in envelope.get("exchanges", []))
        if (actual_http < 1 or wire_http != actual_http or proof["before_http_count"] != actual_http or proof["after_http_count"] != actual_http):
            return None
        for path in transports:
            source_files[path.relative_to(root).as_posix()] = path
        return {"kind": "public_new_default_permissions_lost", "product_status": "failed", "tool_contract_passed": True,
            "fact_contract_status": "failed", "ledger_stopped": True, "guard_error": guard_error,
            "initial_task_id": first.task.id, "new_task_id": changed.task.id,
            "original_scenario_root": str(original_root), "actual_terminal_http_before": actual_http, "actual_terminal_http_after": actual_http,
            "new_chat_http_attempts": 0, "saved_time_only_differences": saved_time_only,
            "source_originals": {name: sha256(path.read_bytes()).hexdigest() for name, path in sorted(source_files.items())}}
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return None
