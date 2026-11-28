"""Preserve formal desktop originals and derive scenario outcomes."""

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal

from myrk.provider import ProviderConfig
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, IncompleteEvidenceError, SharedControlError
from tests.support.agent_acceptance.preparation import LiveAuthorization, LiveGate, PreparedBundle, SOURCE
from tests.support.agent_acceptance.stop_request_evidence import StopExchange, stop_reservations


def preserve_runtime(context: CaseContext, runtime: Path, accounting_root: Path) -> dict[str, str]:
    exact_relocations: dict[str, str] = {}
    long_path_originals = []
    for path in runtime.rglob("*"):
        if path.is_file() and not any(part in {"profile", "session", "cache"} for part in path.relative_to(runtime).parts):
            target = context.root / path.relative_to(runtime)
            if context.case_id == "formal.recovery-unknown" and path.name == "save-diagnostics.json" and path.stat().st_size == 0:
                target = target.with_suffix(".json.incomplete-original")
                exact_relocations[str(path)] = target.relative_to(context.root).as_posix()
            if len(str(target)) >= 248:
                target = context.root / "long-path-originals" / (sha256(path.relative_to(runtime).as_posix().encode()).hexdigest()[:24] + path.suffix)
                exact_relocations[str(path)] = target.relative_to(context.root).as_posix()
                long_path_originals.append({"source_path":str(path),"logical_path":path.relative_to(runtime).as_posix(),
                    "stored_path":target.relative_to(context.root).as_posix(),"sha256":sha256(path.read_bytes()).hexdigest(),"bytes":path.stat().st_size})
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    if long_path_originals:
        (context.root / "long-path-originals.json").write_text(json.dumps(long_path_originals,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    for name in ("prepared.json", "ledger.json"):
        try:
            (context.root / name).write_bytes((accounting_root / name).read_bytes())
        except OSError as error:
            raise SharedControlError(f"Formal accounting original cannot be preserved: {name}: {error}") from error
    if context.live_gate is not None:
        ledger = context.live_gate._read_ledger()
        if ledger.candidates:
            import zipfile
            snapshots = [accounting_root, *(accounting_root / ".candidates" / item["directory"] for item in ledger.candidates)]
            from tests.support.agent_acceptance.preparation import PreparedBundle
            with zipfile.ZipFile(context.root / "accounting.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                for source in snapshots:
                    prepared = PreparedBundle.model_validate_json((source / "prepared.json").read_bytes())
                    for name in (*prepared.files, "prepared.json", *(("candidate-review.json",) if prepared.candidate_directory else ())):
                        archive.writestr((source.relative_to(accounting_root) / name).as_posix(), (source / name).read_bytes())
                archive.writestr("ledger.json", (accounting_root / "ledger.json").read_bytes())
    return exact_relocations


def formal_evidence(root: Path) -> dict[str, Path]:
    return {path.relative_to(root).as_posix(): path for path in root.rglob("*")
        if path.is_file() and not any(part in {"temp", "local", "roaming", "cache"} for part in path.relative_to(root).parts)}


def read_phase_reports(runtime: Path, name: str) -> dict[str, dict[str, Any]]:
    reports = {}
    for path in runtime.glob(f"*/{name}"):
        try:
            report = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            raise SharedControlError(f"Formal control original cannot be read: {path}: {error}") from error
        if not isinstance(report, dict):
            raise SharedControlError(f"Formal control original must be an object: {path}")
        reports[path.relative_to(runtime).parts[0]] = report
    return reports


def score_scenario(
    context: CaseContext, gate: LiveGate, bundle: PreparedBundle, grant: LiveAuthorization, *,
    runtime: Path, phases: list[str], phase_results: list[dict[str, Any]],
    exact_relocations: dict[str, str], config: ProviderConfig,
) -> CaseOutcome:
    protocol = context.protocol
    from tests.support.agent_acceptance.formal_scenarios import score
    records = [json.loads(path.read_bytes()) for path in (runtime / "records").rglob("*.json")
        if path.parent == runtime / "records" or path.parent.name.endswith("-delegations")]
    exchange_paths = runtime.glob("*/receiver/exchange.json")
    invalid_exchanges: list[str] = []
    exchanges: list[dict[str, Any]]
    stop_case = context.case_id == "quality.qg3.formal.9"
    stop_exchange = None
    if stop_case:
        from tests.support.agent_acceptance.preparation import _LiveLedger
        from tests.support.agent_acceptance.quality_governance import read_stop_original
        from tests.support.agent_acceptance.reporting import validate_exchange_header

        exchanges = []
        parsed = []
        for path in exchange_paths:
            label = path.relative_to(runtime).as_posix()
            exchange = read_stop_original(path)
            if not isinstance(exchange, dict) or not validate_exchange_header(context, label, exchange):
                invalid_exchanges.append(label)
                continue
            observation = StopExchange.from_original(exchange)
            if observation.missing_sections:
                invalid_exchanges.append(label)
            exchanges.append(exchange)
            parsed.append(observation)
        if len(parsed) == 1:
            try:
                ledger = _LiveLedger.model_validate_json((context.root / "ledger.json").read_bytes())
            except (OSError, ValueError) as error:
                raise SharedControlError(f"Formal stop ledger cannot be read: {error}") from error
            stop_exchange = replace(parsed[0], reservations=stop_reservations(context, ledger))
    else:
        exchanges = [json.loads(path.read_bytes()) for path in exchange_paths]
    request_budget = None
    if context.case_id.startswith("quality.qg4."):
        accounting = gate.accounting_report()
        selected = [item for item in accounting["attempts"] if item["execution_id"] == context.execution_id]
        owners = accounting["product_request_owners"]
        projected_requests = [{**item, "task_id": owners[str(item["number"])]["task_id"] if str(item["number"]) in owners else None}
            for item in selected]
        request_budget = {"origin": "validated_controller_ledger", "scope": gate.registry.cases[context.case_id].product_stop_scope, "observed_at": datetime.now(timezone.utc).isoformat(),
            "binding": accounting["binding"], "execution_id": context.execution_id, "case_id": context.case_id, "protocol": protocol,
            "frozen_request_cap": context.execution.request_cap if context.execution is not None else bundle.budget_proposals[context.execution_id].request_limit,
            "approved_request_cap": grant.case_limits[context.execution_id], "reserved_requests": len(selected),
            "requests": projected_requests}
    facts = score(context.case_id, runtime, records, exchanges, request_budget=request_budget, stop_exchange=stop_exchange)
    required = [runtime / phase / name for phase in phases for name in
        ("processes.json", "receiver/state.json", "receiver/events.json", "receiver/exchange.json", "receiver/transport.json", "owner/state.json", "relay/state.json")]
    incomplete = [*invalid_exchanges, *(path.relative_to(runtime).as_posix() for path in required if not path.is_file())]
    transport_reports = read_phase_reports(runtime, "receiver/transport.json")
    control_errors = [report["control_error"] for report in transport_reports.values() if report.get("control_error")]
    if control_errors:
        recognized = None
        if context.case_id.startswith("quality."):
            from tests.support.agent_acceptance.quality_runtime import recognize_default_permission_loss
            recognized = recognize_default_permission_loss(context.case_id, runtime, protocol,
                ledger=json.loads((gate.root / "ledger.json").read_bytes()), exchanges=exchanges)
        if (recognized is None or recognized.get("kind") != "public_new_default_permissions_lost"
                or recognized.get("product_status") != "failed" or recognized.get("tool_contract_passed") is not True
                or recognized.get("fact_contract_status") != "failed" or recognized.get("ledger_stopped") is not True
                or facts.get("tool_contract_passed") is not True or facts.get("passed") is not False
                or facts.get("critical_contracts", {}).get("K1") != "failed" or facts.get("driver_errors")):
            raise SharedControlError(f"Formal send control stopped the run: {control_errors}; originals: {context.root}")
        (context.root / "formal-original-control-verdict.json").write_text(json.dumps({
            "kind": "original_unclassified_shared_control_stop", "product_status": "failed", "tool_status": "failed",
            "control_errors": control_errors, "facts": facts,
            "ledger_sha256": sha256((gate.root / "ledger.json").read_bytes()).hexdigest()},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        facts["known_default_permission_loss"] = recognized
        (context.root / "known-default-permission-loss.json").write_text(json.dumps(recognized,
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    process_reports = read_phase_reports(runtime, "processes.json")
    if process_reports.keys() - set(phases) or any(item.get("complete") is not True for item in process_reports.values()):
        raise SharedControlError(f"Owned formal process exit is unconfirmed; original runtime retained: {runtime}")
    if incomplete:
        if stop_case:
            from tests.support.agent_acceptance.reporting import validate_evidence

            control = gate.accounting_report()
            if control.get("stopped"):
                raise SharedControlError(control.get("stop_reason") or "Shared control stopped.")
            facts["evidence_relocation"] = {**exact_relocations, str(runtime): "."}
            evidence = formal_evidence(context.root)
            evidence["budget.ledger"] = context.root / "ledger.json"
            outcome = CaseOutcome("failed", facts, evidence, critical_contracts=facts["critical_contracts"])
            try:
                validate_evidence(context, outcome, missing_originals=incomplete)
            except IncompleteEvidenceError as error:
                raise IncompleteEvidenceError(f"Formal phase evidence is incomplete: {error}; originals retained at {context.root}",
                    stop_violation=error.stop_violation) from error
            except (OSError, ValueError, TypeError, KeyError, StopIteration) as error:
                raise SharedControlError(f"Formal phase evidence cannot be validated: {error}") from error
        raise IncompleteEvidenceError(f"Formal phase evidence is incomplete ({', '.join(incomplete)}); originals retained at {context.root}")
    facts.update(compiled_sources_match=True, transport=grant.transport, mode=context.mode,
        runtime_versions=[json.loads((runtime / phase / "receiver/state.json").read_bytes())["runtime_versions"] for phase in phases],
        phases=phase_results, processes_complete=True,
        desktop_exits={
            phase: {"root_exit": item.get("root_exit"), "failure": item.get("failure")}
            for phase, item in process_reports.items()
        },
        real_http_attempts=sum(report["real_http_attempts"] for report in transport_reports.values()),
        mock_http_attempts=sum(report["mock_http_attempts"] for report in transport_reports.values()),
        planned_local_transport_failures=sum(report["planned_local_transport_failures"] for report in transport_reports.values()),
        evidence_relocation={**exact_relocations, str(runtime): "."})
    from tests.support.agent_acceptance.formal_save import recognize_runtime
    current_failure = recognize_runtime(context, runtime=runtime, bundle=bundle, grant=grant,
        ledger_path=gate.root / "ledger.json", records=records, exchanges=exchanges, facts=facts, source=SOURCE)
    if current_failure is not None:
        (context.root / "formal-original-failed-verdict.json").write_text(json.dumps({
            "kind": "original_unclassified_formal_verdict", "case_id": context.case_id, "protocol": context.protocol,
            "product_status": "failed", "tool_status": "failed", "facts": facts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (context.root / "current-formal-save-observation.json").write_text(json.dumps(current_failure,
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if context.case_id.startswith(("formal.compaction-", "quality.qg6.")) and records:
        from myrk.conversation import ConversationRecord
        from tests.support.agent_acceptance.compaction_review import write_review
        from tests.support.agent_acceptance.replay import OfflineSession

        record = ConversationRecord.model_validate(records[0])
        captured = OfflineSession(protocol, [], config=config)
        captured.sdk_inputs = [item for envelope in exchanges for item in envelope["sdk_inputs"]]
        captured.exchanges = [item for envelope in exchanges for item in envelope["exchanges"]]
        captured.save(context.root / "exchange.json", attempts=[item for envelope in exchanges for item in envelope["product_attempts"]],
            run_id=context.run_id, case_id=context.case_id, task_id=record.turns[-1].task.id if record.turns and record.turns[-1].task else "")
        write_review(context, record, captured, provenance="current_real" if grant.transport == "real" else "fixed_fixture")
    evidence = formal_evidence(context.root)
    facts["budget"] = gate.accounting_report()
    outcome = scenario_outcome(facts, evidence, current_failure=current_failure, expected_phases=len(phases))
    outcome.execution_id = gate.execution_id(context.case_id, protocol)
    if context.case_id.startswith("quality."):
        from tests.support.agent_acceptance.quality_runtime import finish as finish_quality
        finish_quality(context, outcome)
    if context.case_id in {"formal.command", "formal.calculation-check", "formal.files"}:
        from tests.support.agent_acceptance.files_calculation import finish
        finish(context, outcome, facts.get("domain", {"actual_file_bytes_verified": facts["passed"], "final_sha256": facts.get("final_bytes_sha256")}))
    from tests.support.agent_acceptance import compaction_continuity
    if context.case_id in compaction_continuity.FORMAL_CASES:
        compaction_continuity.finish(context,outcome)
    from tests.support.agent_acceptance import delegation_parent_check
    if context.case_id in delegation_parent_check.FORMAL_CASES:
        delegation_parent_check.finish(context, outcome)
    from tests.support.agent_acceptance import memory_isolation
    if context.case_id in memory_isolation.IMPLEMENTED:
        memory_isolation.finish(context, outcome)
    from tests.support.agent_acceptance import queue_parallel_stop
    if context.case_id in queue_parallel_stop.SCENARIOS:
        queue_parallel_stop.finish(context, outcome)
    from tests.support.agent_acceptance import independent_review
    if context.case_id in independent_review.FORMAL_CASES:
        independent_review.finish(context, outcome)
    from tests.support.agent_acceptance import formal_approval
    if context.case_id in formal_approval.FORMAL_CASES:
        formal_approval.finish(context, outcome)
    from tests.support.agent_acceptance.guidance_skills_extensions import SCENARIOS as GUIDANCE_SCENARIOS, finish as finish_guidance
    if context.case_id in GUIDANCE_SCENARIOS:
        finish_guidance(context, outcome)
    from tests.support.agent_acceptance.authorized_send import SCENARIOS as SEND_SCENARIOS, finish as finish_send
    if context.case_id in SEND_SCENARIOS:
        finish_send(context, outcome)
    from tests.support.agent_acceptance import save_import_recovery
    if context.case_id in save_import_recovery.SCENARIOS:
        save_import_recovery.finish(context, outcome)
    from tests.support.agent_acceptance import deliverables
    if context.case_id in deliverables.SCENARIOS:
        deliverables.finish(context, outcome)
    if context.case_id == "formal.provider-settings":
        from tests.support.agent_acceptance.connection_desktop_lifecycle import finish as finish_connection
        finish_connection(context, outcome)
    return outcome


def scenario_outcome(facts: dict[str, Any], evidence: dict[str, Path], *, current_failure: dict[str, Any] | None = None,
                     expected_phases: int | None = None) -> CaseOutcome:
    """Keep raw product and driver facts when constructing a formal scenario outcome."""
    phases: list[dict[str, Any]] = facts["phases"]
    desktop_exit_passed = (
        facts["processes_complete"] is True and (expected_phases is None or len(phases) == expected_phases)
        and all(item["exit_code"] == 0 for item in phases)
    )
    exits: dict[str, dict[str, Any]] | None = facts.get("desktop_exits")
    if exits is not None:
        desktop_exit_passed = (
            desktop_exit_passed and set(exits) == {item["phase"] for item in phases}
            and all(item["root_exit"] == {"code": 0, "signal": None} and item["failure"] is None for item in exits.values())
        )
    facts = {**facts, "desktop_exit_passed": desktop_exit_passed}
    passed = facts["passed"] and desktop_exit_passed
    status: Literal["passed", "failed", "inconclusive", "not_executed"] = "not_executed" if facts.get("product_execution") == "not_executed" else "passed" if passed else "failed"
    tool_status: Literal["passed", "failed"] = (
        "failed" if not desktop_exit_passed or facts.get("tool_contract_passed") is False or facts.get("driver_errors") else "passed"
    )
    if (status == "failed" and facts.get("fact_contract_status") == "unknown" and facts.get("goal_program_contract_passed")
            and tool_status == "passed"):
        status = "inconclusive"
    from tests.support.agent_acceptance.formal_save import known_failure
    if status == "failed" and known_failure(current_failure):
        facts.update(current_save_failure=current_failure, original_formal_verdict={"product_status": status, "tool_status": tool_status})
        if desktop_exit_passed:
            facts["tool_contract_passed"] = True
            tool_status = "passed"
    return CaseOutcome(status, facts, evidence, tool_status=tool_status)
